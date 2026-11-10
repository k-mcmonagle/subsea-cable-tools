# -*- coding: utf-8 -*-
"""Missing Ground Model sources can be repaired without losing imported data."""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtWidgets import QApplication, QDialog, QMessageBox

from ..burial import cpt_register, numeric_datasets, numeric_profiles, schema, source_links
from ..burial.cpt_import_dialog import CptImportDialog
from ..burial.cpt_panel import CptPanel
from ..burial.ground_plot import GroundModelPlot
from ..burial.plan_model import PlanModel
from ..burial.source_links_dialog import GroundSourcesDialog
from ..burial.store import BurialStore
from .test_burial_cpt_qgis import _AGS

REQUIRES_QGIS = True


class GroundSourceRepairTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix='ground_sources_'))
        self.project = QgsProject.instance()
        self.previous_project_file = self.project.fileName()
        self.project.setFileName(str(self.folder / 'project.qgs'))
        self.store = BurialStore(str(self.folder / 'plans.gpkg'))
        self.store.migrate()
        self.plan_id = self.store.save_plan({'name': 'Synthetic source repair', 'method': 'plough'})
        self.model = PlanModel(self.store)
        self.model.load_plan(self.plan_id)
        self.windows, self.layers = [], []
        self.profiles = numeric_profiles.import_profiles(
            [['A', 0, 3], ['A', .02, ''], ['A', .04, 250]],
            {'source_id': 0, 'depth': 1, 'value': 2}, dataset_id='d1', variable='Su', units='kPa')
        self.dataset = {'dataset_id': 'd1', 'name': 'CPT profiles', 'variable': 'Su', 'units': 'kPa', 'config': {
            'measurements': {'source': {'kind': 'file', 'path': str(self.folder / 'old' / 'measurements.xlsx'),
                                        'sheet': 'Readings'},
                             'columns': {'source_id': 'Investigation', 'depth': 'Depth', 'value': 'Strength'},
                             'fingerprint': 'original-import-stamp', 'depth_scale': 1.0, 'point_support_m': .02},
            'colours': {'mode': 'classes', 'classes': [{'min': 0, 'max': 10, 'colour': '#ff0000'}]}}}
        self.store.save_ground_dataset(self.dataset, self.profiles)
        self.workbook = self.make_workbook()

    def tearDown(self):
        for window in self.windows:
            window.hide()
            window.deleteLater()
        self.model.close_plan()
        self.store.close()
        for layer_id in self.layers:
            self.project.removeMapLayer(layer_id)
        QApplication.processEvents()
        self.project.setFileName(self.previous_project_file)
        shutil.rmtree(self.folder, ignore_errors=True)

    def make_workbook(self, sheet='Readings', columns=('Investigation', 'Depth', 'Strength'), name='measurements.xlsx'):
        # The plugin uses openpyxl's pure XML backend in QGIS.
        os.environ['OPENPYXL_LXML'] = 'False'
        import openpyxl
        book = openpyxl.Workbook()
        book.active.title = sheet
        book.active.append(list(columns))
        book.active.append(['A', 0.1, 12.0])
        path = self.folder / name
        book.save(path)
        book.close()
        return str(path)

    def dialog(self):
        dialog = GroundSourcesDialog(self.model)
        self.windows.append(dialog)
        return dialog

    def measurement_link(self, dialog):
        return next(link for link in dialog.links if link.role == 'Measurements file')

    def campaign(self):
        files = []
        for name in ('CPT-A.ags', 'CPT-B.ags'):
            path = self.folder / name
            path.write_text(_AGS)
            files.append(str(path))
        config = {'files': [{'path': path} for path in files], 'name_rule': 'auto'}
        built = cpt_register.build(config, campaign_id='c1')
        self.store.save_cpt_campaign({'campaign_id': 'c1', 'name': 'Synthetic CPTs',
                                     'config': cpt_register.stamp_fingerprints(config)},
                                    built['cpts'], built['readings'])
        self.model.cpts.invalidate()
        return files

    def test_relink_workbook_preserves_every_sample_and_all_import_choices(self):
        before = self.store.list_numeric_profiles('d1')
        dialog = self.dialog()
        link = self.measurement_link(dialog)
        self.assertEqual(source_links.link_status(link), 'Missing original')
        dialog.stage(link, self.workbook)
        with patch.object(self.store, 'replace_numeric_profiles', side_effect=AssertionError('must retain samples')):
            dialog._save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.store.list_numeric_profiles('d1'), before)
        (saved,) = self.store.list_ground_datasets()
        original_config = deepcopy(self.dataset['config'])
        saved_source = deepcopy(saved['config']['measurements']['source'])
        self.assertEqual(source_links.file_path(saved_source['path']), self.workbook)
        self.assertEqual(saved_source['path'], self.project.writePath(self.workbook))
        saved['config']['measurements']['source']['path'] = original_config['measurements']['source']['path']
        self.assertEqual(saved['config'], original_config)
        grid, sheets = numeric_datasets.read_source_grid(saved_source, self.model)
        self.assertIn('Readings', sheets)
        self.assertEqual(grid[0], ['Investigation', 'Depth', 'Strength'])

    def test_cancel_does_not_change_the_dataset_or_samples(self):
        before = self.store.list_ground_datasets(), self.store.list_numeric_profiles('d1')
        dialog = self.dialog()
        dialog.stage(self.measurement_link(dialog), self.workbook)
        dialog.reject()
        self.assertEqual((self.store.list_ground_datasets(), self.store.list_numeric_profiles('d1')), before)

    def test_wrong_sheet_and_columns_are_rejected_before_any_link_change(self):
        dialog = self.dialog()
        link = self.measurement_link(dialog)
        wrong_sheet = self.make_workbook(sheet='Other', name='other.xlsx')
        with self.assertRaisesRegex(ValueError, 'worksheet'):
            dialog.stage(link, wrong_sheet)
        wrong_columns = self.make_workbook(columns=('Investigation', 'Depth', 'Other'), name='columns.xlsx')
        with self.assertRaisesRegex(ValueError, 'Strength'):
            dialog.stage(link, wrong_columns)
        self.assertEqual(dialog.pending, {})
        with self.assertRaisesRegex(ValueError, 'worksheet'):
            numeric_datasets.read_source_grid({'kind': 'file', 'path': wrong_sheet, 'sheet': 'Readings'})

    def test_live_placement_can_be_relinked_independently_of_measurements(self):
        before = self.store.list_numeric_profiles('d1')
        dataset = deepcopy(self.dataset)
        dataset['config']['placement'] = {'kind': 'polygons', 'id_field': 'Investigation',
            'layer': {'layer_source': str(self.folder / 'missing.gpkg') + '|layername=areas'}}
        self.store.save_ground_dataset(dataset)
        layer = QgsVectorLayer('Polygon?crs=EPSG:4326&field=Investigation:string', 'Replacement polygons', 'memory')
        self.project.addMapLayer(layer)
        self.layers.append(layer.id())
        dialog = self.dialog()
        link = next(link for link in dialog.links if link.usage == 'Live placement')
        dialog.stage(link, numeric_datasets.layer_ref(layer))
        dialog._save()
        (saved,) = self.store.list_ground_datasets()
        self.assertEqual(numeric_datasets.resolve_layer(saved['config']['placement']['layer']).id(), layer.id())
        self.assertEqual(self.store.list_numeric_profiles('d1'), before)

    def test_cpt_relink_changes_metadata_only_and_preserves_import_timestamp(self):
        files = self.campaign()
        before = self.store.list_cpts(), self.store.cpt_readings()
        imported = self.store.list_cpt_campaigns()[0]['imported_utc']
        target = self.folder / 'moved'
        target.mkdir()
        moved = target / Path(files[0]).name
        os.rename(files[0], moved)
        dialog = self.dialog()
        link = next(link for link in dialog.links if link.role == 'AGS file' and link.value == files[0])
        dialog.stage(link, str(moved))
        dialog._save()
        self.assertEqual((self.store.list_cpts(), self.store.cpt_readings()), before)
        saved = self.store.list_cpt_campaigns()[0]
        self.assertEqual(saved['imported_utc'], imported)
        self.assertEqual(cpt_register.changed_sources(saved), [])
        rebuilt = cpt_register.build(saved['config'], campaign_id='c1')
        self.assertEqual(len(rebuilt['cpts']), 2)

    def test_partial_reload_and_edit_cannot_remove_cpts_for_an_unreadable_file(self):
        files = self.campaign()
        before = self.store.list_cpts(), self.store.cpt_readings()
        Path(files[1]).unlink()
        panel = CptPanel(self.model, None, GroundModelPlot())
        self.windows.append(panel)
        panel.refresh()
        panel.campaign.setCurrentIndex(panel.campaign.findData('c1'))
        with patch.object(QMessageBox, 'warning') as warning:
            panel._reload()
        self.assertTrue(warning.called)
        self.assertIn('kept', warning.call_args[0][2])
        self.assertEqual((self.store.list_cpts(), self.store.cpt_readings()), before)
        campaign = self.store.list_cpt_campaigns()[0]
        dialog = CptImportDialog(self.model, campaign)
        self.windows.append(dialog)
        with patch.object(QMessageBox, 'warning'):
            dialog._accept()
        self.assertIsNone(dialog.result())
        self.assertEqual((self.store.list_cpts(), self.store.cpt_readings()), before)

    def test_outdated_dialog_does_not_overwrite_a_newer_dataset_edit(self):
        dialog = self.dialog()
        link = self.measurement_link(dialog)
        dialog.stage(link, self.workbook)
        (edited,) = self.store.list_ground_datasets()
        edited['config']['colours']['mode'] = 'continuous'
        self.store.save_ground_dataset(edited)
        errors = []
        self.model.storeError.connect(errors.append)
        dialog._save()
        self.assertTrue(errors)
        (saved,) = self.store.list_ground_datasets()
        self.assertEqual(saved['config'], edited['config'])


    def test_damaged_workbook_reports_a_plain_error_and_keeps_saved_samples(self):
        path = self.folder / 'damaged.xlsx'
        path.write_text('This is not an Excel workbook')
        before = self.store.list_numeric_profiles('d1')
        dialog = self.dialog()
        with self.assertRaisesRegex(ValueError, 'could not be read'):
            dialog.stage(self.measurement_link(dialog), str(path))
        with self.assertRaisesRegex(ValueError, 'could not be read'):
            numeric_datasets.read_source_grid({'kind': 'file', 'path': str(path)})
        self.assertEqual(dialog.pending, {})
        self.assertEqual(self.store.list_numeric_profiles('d1'), before)

    def test_check_window_has_a_direct_source_repair_action(self):
        from qgis.PyQt.QtWidgets import QPushButton
        from ..burial.numeric_profile_dialogs import check_dialog
        calls = []
        dialog = check_dialog(self.dataset, self.profiles, [], [], None, None, None,
                              on_sources=lambda: calls.append('sources'))
        self.windows.append(dialog)
        button = next(button for button in dialog.findChildren(QPushButton) if button.text() == 'Sources…')
        button.click()
        self.assertEqual(calls, ['sources'])
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_bulk_repair_failure_rolls_back_the_first_metadata_change(self):
        files = self.campaign()
        replacement = self.folder / 'new_ags'
        replacement.mkdir()
        moved = replacement / Path(files[0]).name
        shutil.copy2(files[0], moved)
        dialog = self.dialog()
        ags = next(link for link in dialog.links if link.role == 'AGS file' and link.value == files[0])
        dialog.stage(ags, str(moved))
        dialog.stage(self.measurement_link(dialog), self.workbook)
        before = self.store.list_cpt_campaigns(), self.store.list_ground_datasets(), self.store.cpt_readings()
        errors = []
        self.model.storeError.connect(errors.append)
        with patch.object(self.store, 'save_ground_dataset', side_effect=RuntimeError('simulated write failure')):
            dialog._save()
        self.assertTrue(errors)
        self.assertEqual((self.store.list_cpt_campaigns(), self.store.list_ground_datasets(), self.store.cpt_readings()), before)

    def test_repair_refuses_a_file_upgraded_while_the_dialog_was_open(self):
        dialog = self.dialog()
        dialog.stage(self.measurement_link(dialog), self.workbook)
        before = self.store.list_ground_datasets(), self.store.list_numeric_profiles('d1')
        self.store.write_meta('schema_version', str(schema.SCHEMA_VERSION + 1))
        errors = []
        self.model.storeError.connect(errors.append)
        dialog._save()
        self.assertTrue(errors)
        self.assertEqual((self.store.list_ground_datasets(), self.store.list_numeric_profiles('d1')), before)

    def test_folder_search_stages_matching_files_and_waits_for_save(self):
        from qgis.PyQt.QtWidgets import QFileDialog
        before = self.store.list_ground_datasets(), self.store.list_numeric_profiles('d1')
        dialog = self.dialog()
        link = self.measurement_link(dialog)
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=str(self.folder)):
            dialog._folder()
        self.assertEqual(dialog.pending[link.key], self.workbook)
        self.assertEqual((self.store.list_ground_datasets(), self.store.list_numeric_profiles('d1')), before)
        dialog._save()
        self.assertEqual(self.store.list_numeric_profiles('d1'), before[1])
        (saved,) = self.store.list_ground_datasets()
        self.assertEqual(source_links.file_path(saved['config']['measurements']['source']['path']), self.workbook)
