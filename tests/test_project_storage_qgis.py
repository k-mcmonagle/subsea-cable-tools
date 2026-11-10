# -*- coding: utf-8 -*-
"""QGIS regression checks for new-file choices and reopening manually moved stores."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qgis.core import QgsPathResolver, QgsProject, QgsVectorLayer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QFileDialog, QInputDialog

from .. import project_storage as storage
from ..burial import schema as burial_schema
from ..burial.plan_model import PlanModel
from ..burial.store import BurialStore, project_gpkg_path, set_project_gpkg_path
from ..processing.cable_lay_parsers import WKT_KEY
from ..qgis_compat import WKB_POINT
from ..workbench.store import WorkbenchStore
from .test_burial_project_switch import _Env
from .test_workbench_project_switch import _Iface

REQUIRES_QGIS = True


class ProjectStorageQgisTests(unittest.TestCase):
    def setUp(self):
        # OGR pools file handles on Windows; let OS temp cleanup remove these.
        self.root = Path(tempfile.mkdtemp(prefix='sct_storage_'))
        self.env = _Env(str(self.root))
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        self.project = QgsProject.instance()
        self.project.setFileName(str(self.root / 'survey.qgs'))

    def burial_store(self, folder='burial_plans', filename='plans.gpkg'):
        store = BurialStore(str(self.root / folder / filename))
        store.migrate()
        plan_id = PlanModel(store).create_plan('Saved plan', 'plough')
        store.write_spatial_layer('probe', [('name', 'str')], WKB_POINT,
                                  [{'name': 'saved', WKT_KEY: 'POINT (0 0)'}])
        store.close()
        return store, plan_id

    def broken_layer(self, target, missing):
        layer = QgsVectorLayer(target + '|layername=probe', 'Saved points', 'ogr')
        self.assertTrue(layer.isValid())
        layer.renderer().symbol().setColor(QColor('#c34567'))
        self.project.addMapLayer(layer)
        layer.setDataSource(missing + '|layername=probe', layer.name(), 'ogr')
        self.assertFalse(layer.isValid())
        return layer

    def test_explicit_reopen_repairs_ids_styles_and_captured_inputs(self):
        store, _plan_id = self.burial_store(folder='client_layout', filename='renamed.gpkg')
        missing = str(self.root / 'original.gpkg')
        set_project_gpkg_path(missing)
        layer = self.broken_layer(store.gpkg_path, missing)
        layer_id = layer.id()
        color = layer.renderer().symbol().color().name()
        self.assertEqual(storage.repair_project_layers(self.project, missing, store.gpkg_path), 1)
        self.assertEqual(layer.id(), layer_id)
        self.assertEqual(layer.renderer().symbol().color().name(), color)
        self.assertTrue(layer.isValid())
        self.assertEqual(len(self.project.mapLayers()), 1)
        self.assertEqual(storage.resolve_source(self.project, missing + '|layername=probe'),
                         store.gpkg_path + '|layername=probe')
        self.assertFalse(Path(missing).exists())

    def test_saved_project_load_finds_conventional_subfolder_before_bad_layers(self):
        store, _plan_id = self.burial_store()
        layer = QgsVectorLayer(store.gpkg_path + '|layername=probe', 'Saved points', 'ogr')
        self.project.addMapLayer(layer)
        layer_id = layer.id()
        set_project_gpkg_path(str(self.root / 'plans.gpkg'))
        qgs = self.root / 'survey.qgs'
        self.assertTrue(self.project.write(str(qgs)))
        # Model the project file saved before the user moved its GeoPackage.
        qgs.write_text(qgs.read_text(encoding='utf-8').replace('burial_plans/plans.gpkg', 'plans.gpkg'), encoding='utf-8')
        self.project.clear()
        hook = storage.install_path_preprocessor()
        try:
            self.assertTrue(self.project.read(str(qgs)))
            loaded = self.project.mapLayer(layer_id)
            self.assertIsNotNone(loaded)
            self.assertTrue(loaded.isValid())
            self.assertEqual(os.path.normcase(loaded.source().split('|')[0]), os.path.normcase(store.gpkg_path))
            self.assertEqual(project_gpkg_path(), store.gpkg_path)
            self.assertFalse((self.root / 'plans.gpkg').exists())
        finally:
            QgsPathResolver.removePathPreprocessor(hook)

    def test_burial_missing_file_is_quiet_and_open_existing_repairs_it(self):
        store, plan_id = self.burial_store(folder='chosen_location', filename='renamed.gpkg')
        missing = str(self.root / 'original.gpkg')
        set_project_gpkg_path(missing)
        layer = self.broken_layer(store.gpkg_path, missing)
        dock = self.env.dock()
        self.assertFalse(dock.store_ready)
        self.assertFalse(Path(missing).exists())
        self.assertFalse(self.env.warnings)
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(store.gpkg_path, '')):
            dock._pick_gpkg()
        self.assertTrue(dock.store_ready)
        self.assertEqual(dock.model.plan_id, plan_id)
        self.assertEqual(project_gpkg_path(), store.gpkg_path)
        self.assertTrue(layer.isValid())
        self.assertEqual(sum(item.id() == layer.id() for item in self.project.mapLayers().values()), 1)

    def test_first_burial_plan_chooses_file_once_and_cancel_writes_nothing(self):
        dock = self.env.dock()
        self.assertFalse(dock.store_ready)
        with patch.object(QInputDialog, 'getText', return_value=('New plan', True)), \
                patch.object(QFileDialog, 'getSaveFileName', return_value=('', '')):
            dock._new_plan()
        self.assertFalse(dock.store_ready)
        self.assertIsNone(project_gpkg_path())
        chosen = str(self.root / 'my_layout' / 'selected.gpkg')
        with patch.object(QInputDialog, 'getText', return_value=('New plan', True)), \
                patch.object(QFileDialog, 'getSaveFileName', return_value=(chosen, '')) as chooser:
            dock._new_plan()
            dock._new_plan()
            self.assertEqual(chooser.call_count, 1)
        self.assertEqual(len(dock.store.list_plans()), 2)
        self.assertEqual(project_gpkg_path(), chosen)

    def test_workbench_missing_file_does_not_create_a_replacement(self):
        from ..workbench.workbench_dock import WorkbenchDock
        from ..workbench import store as workbench_store
        target = str(self.root / 'custom' / 'renamed.gpkg')
        WorkbenchStore(target).migrate()
        missing = str(self.root / 'old_workbench.gpkg')
        workbench_store.set_project_gpkg_path(missing)
        iface = _Iface()
        dock = WorkbenchDock(iface)
        try:
            self.assertEqual(dock.rpl_panel.store.gpkg_path, missing)
            self.assertFalse(Path(missing).exists())
            self.assertTrue(dock._activate_workbench(target))
            self.assertEqual(workbench_store.project_gpkg_path(), target)
        finally:
            dock.shutdown()
            dock.deleteLater()

    def test_new_file_cannot_overwrite_existing_data(self):
        store, _plan_id = self.burial_store()
        before = Path(store.gpkg_path).read_bytes()
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(store.gpkg_path, '')):
            self.assertIsNone(storage.choose_new_gpkg(None, 'New file', store.gpkg_path))
        self.assertEqual(Path(store.gpkg_path).read_bytes(), before)

    def test_newer_burial_file_is_rejected_without_replacing_or_modifying_it(self):
        store, _plan_id = self.burial_store()
        store.write_meta('schema_version', str(burial_schema.SCHEMA_VERSION + 1))
        store.close()
        before = Path(store.gpkg_path).read_bytes()
        dock = self.env.dock()
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(store.gpkg_path, '')):
            dock._pick_gpkg()
        self.assertFalse(dock.store_ready)
        self.assertEqual(Path(store.gpkg_path).read_bytes(), before)

    def test_workbench_first_file_choice_can_be_cancelled_and_is_remembered(self):
        from ..workbench.workbench_dock import WorkbenchDock
        from ..workbench import store as workbench_store
        iface = _Iface()
        dock = WorkbenchDock(iface)
        try:
            with patch.object(QFileDialog, 'getSaveFileName', return_value=('', '')):
                self.assertFalse(dock._ensure_workbench_file())
            self.assertIsNone(workbench_store.project_gpkg_path())
            chosen = str(self.root / 'selected' / 'workbench.gpkg')
            with patch.object(QFileDialog, 'getSaveFileName', return_value=(chosen, '')) as chooser:
                self.assertTrue(dock._ensure_workbench_file())
                self.assertTrue(dock._ensure_workbench_file())
                self.assertEqual(chooser.call_count, 1)
            self.assertEqual(workbench_store.project_gpkg_path(), chosen)
            self.assertTrue(Path(chosen).is_file())
        finally:
            dock.shutdown()
            dock.deleteLater()

    def test_planner_open_existing_repairs_task_layer_in_place(self):
        from ..planner.planner_dock import PlannerDock
        from ..planner import store as planner_store, schema as planner_schema
        from .test_planner_project_switch import _Iface as PlannerIface
        target = str(self.root / 'selected' / 'renamed.gpkg')
        store = planner_store.PlannerStore(target)
        store.migrate()
        scenario_id = store.create_scenario('Existing scenario', '2026-01-01T00:00')
        missing = str(self.root / 'old_planner.gpkg')
        layer = QgsVectorLayer(target + '|layername=' + planner_schema.TABLE_TASK_POINT, 'Task points', 'ogr')
        self.project.addMapLayer(layer)
        layer_id = layer.id()
        layer.setDataSource(missing + '|layername=' + planner_schema.TABLE_TASK_POINT, layer.name(), 'ogr')
        planner_store.set_project_gpkg_path(missing)
        iface = PlannerIface()
        dock = PlannerDock(iface)
        try:
            self.assertFalse(dock.store_ready)
            self.assertFalse(Path(missing).exists())
            self.assertFalse(self.env.warnings)
            with patch.object(QFileDialog, 'getOpenFileName', return_value=(target, '')):
                dock._open_planner_file()
            self.assertTrue(dock.store_ready)
            self.assertEqual(dock.current_scenario_id, scenario_id)
            self.assertTrue(self.project.mapLayer(layer_id).isValid())
            matches = [item for item in self.project.mapLayers().values()
                       if item.source().endswith('|layername=' + planner_schema.TABLE_TASK_POINT)]
            self.assertEqual(len(matches), 1)
        finally:
            dock.shutdown()
            dock.deleteLater()

    def test_span_missing_file_cannot_be_recreated_by_save(self):
        from types import SimpleNamespace
        from ..span_analysis.dialog import SpanAnalysisDialog
        from ..span_analysis import store as span_store
        missing = str(self.root / 'missing_span.gpkg')
        span_store.set_project_gpkg_path(missing)
        messages = []
        dialog = SimpleNamespace(_store=None, _set_status=lambda text, **_kw: messages.append(text))
        self.assertIsNone(SpanAnalysisDialog._ensure_store(dialog))
        self.assertFalse(Path(missing).exists())
        self.assertTrue(any('Open' in message for message in messages))

    def test_workbench_discovery_does_not_choose_between_same_name_copies(self):
        from ..workbench import project_layers, store as workbench_store
        WorkbenchStore(str(self.root / 'custom.gpkg')).migrate()
        WorkbenchStore(str(self.root / 'workbench' / 'custom.gpkg')).migrate()
        missing = str(self.root / 'previous' / 'custom.gpkg')
        workbench_store.set_project_gpkg_path(missing)
        self.assertIsNone(project_layers.discover_gpkg_path(self.project))
        self.assertEqual(workbench_store.project_gpkg_path(), missing)

    def test_reopen_after_a_second_move_also_repairs_an_older_project_entry(self):
        store, _plan_id = self.burial_store(folder='my_layout', filename='renamed.gpkg')
        entry = str(self.root / 'original.gpkg')
        previous_layer_path = str(self.root / 'previous_folder' / 'original.gpkg')
        layer = self.broken_layer(store.gpkg_path, previous_layer_path)
        self.assertEqual(storage.repair_project_layers(self.project, entry, store.gpkg_path), 1)
        self.assertTrue(layer.isValid())
        self.assertEqual(storage.resolve_source(self.project, entry + '|layername=probe'),
                         store.gpkg_path + '|layername=probe')
