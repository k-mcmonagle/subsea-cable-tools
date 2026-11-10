# -*- coding: utf-8 -*-
"""Pure checks for project store defaults and manual relocation (no migration)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from .. import project_storage as storage
from ..burial import schema as burial_schema
from ..planner import schema as planner_schema
from ..workbench import schema as workbench_schema

REQUIRES_QGIS = False


class Project:
    def __init__(self, path):
        self.path, self.entries = str(path), {}

    def fileName(self):
        return self.path

    def readEntry(self, _scope, key, default):
        return self.entries.get(key, default), key in self.entries

    def writeEntry(self, _scope, key, value):
        self.entries[key] = value

    def writePath(self, value):
        return './' + os.path.relpath(value, os.path.dirname(self.path)).replace(os.sep, '/')

    def readPath(self, value):
        return storage.absolute_source(self, value)


def database(path, registry='bp_plan', layer='bp_sections'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE gpkg_contents (table_name TEXT)')
        conn.execute('CREATE TABLE "' + registry + '" (name TEXT)')
        conn.execute('CREATE TABLE "' + layer + '" (name TEXT)')
    return str(path)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root / 'survey.qgs')

    def test_defaults_use_tool_subfolders_without_creating_them(self):
        for schema, folder in ((burial_schema, 'burial_plans'), (workbench_schema, 'workbench'),
                               (planner_schema, 'planner')):
            path = schema.default_gpkg_path(self.project.fileName())
            self.assertEqual(Path(path), self.root / folder / f'survey_{folder}.gpkg')
            self.assertFalse((self.root / folder).exists())

    def test_existing_legacy_default_is_kept_even_with_a_competing_copy(self):
        old = self.root / 'survey_burial_plans.gpkg'
        old.touch()
        database(self.root / 'burial_plans' / old.name)
        self.assertEqual(burial_schema.default_gpkg_path(self.project.fileName()), str(old))
        self.assertEqual(Path(burial_schema.default_gpkg_path(self.project.fileName(), prefer_existing=False)),
                         self.root / 'burial_plans' / old.name)
        self.assertTrue(old.exists())

    def test_same_filename_in_subfolder_is_recovered_read_only(self):
        old = str(self.root / 'custom.gpkg')
        new = database(self.root / 'burial_plans' / 'custom.gpkg')
        self.project.entries['burial_gpkg'] = old
        before = Path(new).read_bytes()
        self.assertEqual(storage.read_project_path(self.project, 'burial_gpkg', 'burial_plans'), new)
        self.assertEqual(self.project.entries['burial_gpkg'], old)
        self.assertEqual(Path(new).read_bytes(), before)
        self.assertFalse(Path(old).exists())
        self.assertEqual(sorted(p.name for p in Path(new).parent.iterdir()), ['custom.gpkg'])

    def test_existing_custom_location_is_never_redirected(self):
        old = database(self.root / 'shared' / 'plans.gpkg')
        database(self.root / 'burial_plans' / 'plans.gpkg')
        self.project.entries['burial_gpkg'] = old
        self.assertEqual(storage.read_project_path(self.project, 'burial_gpkg', 'burial_plans'), old)

    def test_missing_file_does_not_create_anything_or_choose_a_different_default(self):
        old = str(self.root / 'missing.gpkg')
        database(self.root / 'burial_plans' / 'survey_burial_plans.gpkg')
        self.project.entries['burial_gpkg'] = old
        self.assertEqual(storage.read_project_path(self.project, 'burial_gpkg', 'burial_plans'), old)
        self.assertFalse(Path(old).exists())

    def test_wrong_store_and_missing_layer_are_rejected(self):
        old = str(self.root / 'plans.gpkg')
        database(self.root / 'burial_plans' / 'plans.gpkg', registry='wb_rpl')
        self.assertEqual(storage.relocated_path(self.project.fileName(), old, 'burial_plans'), old)
        self.assertEqual(storage.relocated_path(self.project.fileName(), old + '|layername=absent'),
                         old + '|layername=absent')

    def test_ambiguous_same_name_copies_require_manual_selection(self):
        old = str(self.root / 'previous' / 'plans.gpkg')
        database(self.root / 'burial_plans' / 'plans.gpkg')
        database(self.root / 'plans.gpkg')
        self.assertEqual(storage.relocated_path(self.project.fileName(), old, 'burial_plans'), old)

    def test_uri_options_and_relative_paths_survive_relocation(self):
        new = database(self.root / 'burial_plans' / 'plans.gpkg')
        suffix = '|layername=bp_sections|subset="name" IS NOT NULL'
        self.assertEqual(storage.relocated_path(self.project.fileName(), './plans.gpkg' + suffix), new + suffix)

    def test_project_entry_roundtrip_and_captured_reference_repair(self):
        new = database(self.root / 'client_layout' / 'renamed.gpkg')
        storage.write_project_path(self.project, 'burial_gpkg', new)
        self.assertTrue(self.project.entries['burial_gpkg'].startswith('./'))
        self.assertEqual(storage.read_project_path(self.project, 'burial_gpkg', 'burial_plans'), new)
        old = str(self.root / 'previous.gpkg')
        self.project.entries[storage.RELOCATIONS_KEY] = json.dumps({old: self.project.writePath(new)})
        self.assertEqual(storage.resolve_source(self.project, old + '|layername=bp_sections'),
                         new + '|layername=bp_sections')
        self.assertEqual(storage.resolve_source(self.project, 'memory:route'), 'memory:route')

    def test_unsaved_project_uses_profile_fallback_without_current_directory(self):
        path = storage.default_gpkg_path('', 'My Project', 'burial_plans', 'burial_plans',
                                         lambda _p: str(self.root), lambda s: s.lower().replace(' ', '_'))
        self.assertEqual(Path(path), self.root / 'burial_plans' / 'my_project_burial_plans.gpkg')

    def test_repeated_manual_moves_follow_project_relocation_chain(self):
        original = str(self.root / 'original.gpkg')
        previous = str(self.root / 'previous_location' / 'plans.gpkg')
        current = database(self.root / 'my_choice' / 'renamed.gpkg')
        self.project.entries[storage.RELOCATIONS_KEY] = json.dumps({
            original: self.project.writePath(previous), previous: self.project.writePath(current)})
        self.assertEqual(storage.resolve_source(self.project, original + '|layername=bp_sections'),
                         current + '|layername=bp_sections')
