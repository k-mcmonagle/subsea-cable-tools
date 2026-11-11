"""Regression coverage for project teardown on QGIS before and after 3.34."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from ..project_lifecycle import (project_teardown_signal, project_teardown_signal_name,
                                 project_teardown_slots)

REQUIRES_QGIS = False


class ProjectLifecycleTests(unittest.TestCase):
    def test_qgis_322_without_about_to_be_cleared(self):
        signal = object()
        project = SimpleNamespace(removeAll=signal, cleared=object())
        self.assertEqual(project_teardown_signal_name(project), 'removeAll')
        self.assertIs(project_teardown_signal(project), signal)

    def test_modern_qgis_keeps_early_signal(self):
        signal = object()
        project = SimpleNamespace(aboutToBeCleared=signal, removeAll=object())
        self.assertEqual(project_teardown_signal_name(project), 'aboutToBeCleared')
        self.assertIs(project_teardown_signal(project), signal)

    def test_legacy_hooks_cover_empty_projects_and_disconnect_symmetrically(self):
        project = SimpleNamespace(removeAll=object(), cleared=object())
        slot = lambda: None
        pairs = project_teardown_slots(project, slot)
        self.assertEqual(pairs, ((project.removeAll, slot), (project.cleared, slot)))
        self.assertEqual(project_teardown_slots(project, slot), pairs)

    def test_modern_cleanup_has_only_one_hook(self):
        project = SimpleNamespace(aboutToBeCleared=object(), removeAll=object(), cleared=object())
        slot = lambda: None
        self.assertEqual(project_teardown_slots(project, slot), ((project.aboutToBeCleared, slot),))

    def test_tools_do_not_access_modern_only_signal_directly(self):
        root = Path(__file__).resolve().parents[1]
        folders = ('maptools', 'burial', 'explorer', 'planner', 'span_analysis', 'workbench')
        paths = list(root.glob('*.py'))
        for folder in folders:
            paths.extend((root / folder).rglob('*.py'))
        for path in paths:
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.Attribute):
                    self.assertNotEqual(node.attr, 'aboutToBeCleared', str(path))
