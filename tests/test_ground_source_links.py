# -*- coding: utf-8 -*-
"""Source inventories and metadata-only link repair rules, without QGIS."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from ..burial import source_links as links

REQUIRES_QGIS = False


class GroundSourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = {"dataset_id": "d1", "name": "Investigation profiles", "config": {
            "measurements": {"source": {"kind": "file", "path": str(self.root / 'missing.csv')},
                             "columns": {"source_id": "Investigation", "depth": "Depth", "value": "Value"},
                             "fingerprint": "original-stamp", "point_support_m": .02},
            "placement": {"kind": "polygons", "layer": {"layer_source": "missing.gpkg|layername=areas"},
                          "id_field": "Investigation", "soil_constraint": {"enabled": True,
                          "layer": {"layer_source": "missing.gpkg|layername=soil"}, "unit_field": "Unit"}},
            "colours": {"mode": "classes", "classes": [{"min": 0, "max": 10, "colour": "#ff0000"}]}}}

    def test_inventory_distinguishes_original_files_live_layers_and_stored_data(self):
        campaign = {"campaign_id": "c1", "name": "CPT import", "config": {
            "files": [{"path": "one.ags"}, {"path": "two.ags"}],
            "positions": {"path": "positions.csv"}}}
        found = links.inventory([campaign], [self.dataset])
        self.assertEqual(sum(link.kind == 'stored' for link in found), 2)
        self.assertEqual(sum(link.kind == 'file' for link in found), 4)
        self.assertEqual(sum(link.usage == 'Live placement' for link in found), 2)
        self.assertEqual(len({link.key for link in found}), len(found))
        self.assertEqual({link.owner_name for link in found}, {'CPT import', 'Investigation profiles'})

    def test_relink_retains_fingerprint_columns_colours_and_placement(self):
        original = deepcopy(self.dataset)
        link = next(link for link in links.inventory([], [self.dataset]) if link.kind == 'file')
        changed = links.with_relink(self.dataset, link, str(self.root / 'new.csv'))
        self.assertEqual(self.dataset, original)
        changed['config']['measurements']['source']['path'] = original['config']['measurements']['source']['path']
        self.assertEqual(changed, original)

    def test_missing_original_is_distinct_from_missing_stored_measurements(self):
        found = links.inventory([], [self.dataset])
        self.assertEqual(links.link_status(next(link for link in found if link.kind == 'stored')), 'Stored')
        self.assertEqual(links.link_status(next(link for link in found if link.kind == 'file')), 'Missing original')

    def test_replacement_must_have_saved_columns(self):
        path = self.root / 'measurements.csv'
        path.write_text('Investigation,Depth,Value\nA,0.2,15\n')
        link = next(link for link in links.inventory([], [self.dataset]) if link.kind == 'file')
        links.validate_link(link, str(path), self.dataset)
        path.write_text('Investigation,Depth,Other\nA,0.2,15\n')
        with self.assertRaisesRegex(ValueError, 'Value'):
            links.validate_link(link, str(path), self.dataset)

    def test_relative_file_is_based_on_the_saved_project(self):
        class Project:
            def fileName(self):
                return str(self_root / 'project.qgs')
        self_root = self.root
        self.assertEqual(links.file_path('./data/measurements.csv', Project()),
                         str(self.root / 'data' / 'measurements.csv'))
