"""Keep the plugin and its bundled Python dependencies usable on Python 3.9."""
import ast
from email.parser import Parser
import importlib.util
from pathlib import Path
import re
import unittest

REQUIRES_QGIS = False
ROOT = Path(__file__).resolve().parents[1]


class RuntimeCompatibilityTests(unittest.TestCase):
    def test_bundled_packages_support_python_39(self):
        for path in (ROOT / 'lib').glob('*.dist-info/METADATA'):
            metadata = Parser().parsestr(path.read_text(encoding='utf-8'))
            requirement = metadata.get('Requires-Python', '')
            minimum = re.search(r'>=\s*(\d+)\.(\d+)', requirement)
            if minimum:
                self.assertLessEqual(tuple(map(int, minimum.groups())), (3, 9),
                                     f'{metadata["Name"]}: {requirement}')

    def test_dependency_pins_match_bundled_versions(self):
        pins = dict(re.findall(r"^([\w-]+)==([^\s#]+)",
                               (ROOT / 'requirements.txt').read_text(), re.MULTILINE))
        for path in (ROOT / 'lib').glob('*.dist-info/METADATA'):
            metadata = Parser().parsestr(path.read_text(encoding='utf-8'))
            self.assertEqual(pins[metadata['Name']], metadata['Version'], str(path))

    def test_plugin_syntax_and_annotations_support_python_39(self):
        skip = {'lib', 'tests', 'scripts', '.git', '.venv', 'venv', 'ref', '.agents', '.codex'}
        for path in ROOT.rglob('*.py'):
            if set(path.relative_to(ROOT).parts) & skip:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), feature_version=(3, 9))
            postponed = any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
                            and any(a.name == 'annotations' for a in node.names)
                            for node in tree.body)
            if postponed:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.AnnAssign, ast.arg, ast.FunctionDef, ast.AsyncFunctionDef)):
                    annotation = getattr(node, 'annotation', None) or getattr(node, 'returns', None)
                    if annotation is not None:
                        self.assertFalse(any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                                             for n in ast.walk(annotation)),
                                         f'{path.relative_to(ROOT)}:{node.lineno}: evaluated union needs Python 3.10')

    def test_bundled_tabulate_mdb_reader_call(self):
        # Load the bundled copy explicitly even if QGIS supplies another version.
        spec = importlib.util.spec_from_file_location('sct_vendored_tabulate', ROOT / 'lib/tabulate/__init__.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = module.tabulate({'id': ['001'], 'name': ['Route']}, headers='keys', disable_numparse=True)
        self.assertIn('001', output)
        self.assertIn('Route', output)
