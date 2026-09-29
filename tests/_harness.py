# -*- coding: utf-8 -*-
"""Test discovery plus a small pytest-compatible harness (standard library only).

Shared by ``tests/run_pure_tests.py``, ``tests/run_qgis_smoke_tests.py`` and
``tests/conftest.py`` so every runner sees the same set of test files:

* **Discovery** - every ``tests/test_*.py`` file is a test module. Nothing is
  hand-listed; a new file is picked up automatically.
* **Styles** - a module is run through its ``run_all()`` function when it has
  one (returns a list of per-test booleans, a failure count, or a list of
  failure names). Otherwise its ``unittest.TestCase`` classes and pytest-style
  ``test_*`` functions are collected. A file with none of those fails loudly.
* **Groups** - ``lay`` (slow catenary / lay-simulator suites: file names
  ``test_v3_*``, ``test_catenary_*``, ``test_drape_*``, or ``TEST_GROUP =
  "lay"``) and ``core`` (everything else); ``--fast`` skips ``lay``.
* **Pure vs QGIS** - a module may declare ``REQUIRES_QGIS = True`` (or
  ``False``) at module level. Without the marker the pure runner imports the
  module with QGIS/Qt blocked: an import that needs QGIS classifies the module
  as QGIS-only (the smoke runner runs it). The smoke runner runs every module.
* **pytest without pytest** - pytest is not installed in the QGIS Pythons, so
  pytest-style modules run on a built-in stand-in (see ``make_fake_pytest``):
  fixtures ``tmp_path``, ``monkeypatch``, ``capsys``, ``request`` and
  module-defined ``@pytest.fixture`` functions; marks ``skip``, ``skipif``,
  ``xfail``, ``parametrize``, ``usefixtures``; ``raises``, ``approx``,
  ``skip``, ``fail``, ``importorskip``, ``param``, ``warns``.
"""

from __future__ import annotations

import ast
import contextlib
import gc
import importlib
import importlib.abc
import importlib.util
import inspect
import io
import math
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import types
import unittest
import warnings
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
PACKAGE_NAME = "subsea_cable_tools"

# The slow "lay" group: catenary solvers and the lay simulator (V2/V3),
# chosen by file-name pattern. `--fast` skips it; run it whenever catenary/
# or the lay-simulator tools (or shared modules they import) change. A module
# outside the pattern can opt in or out with `TEST_GROUP = "lay"` / "core".
LAY_PREFIXES = ("test_v3_", "test_catenary_", "test_drape_")
GROUPS = ("core", "lay")

# Top-level packages that only exist inside a QGIS Python.
QGIS_ROOTS = frozenset({"qgis", "PyQt5", "PyQt6", "PySide2", "PySide6", "sip", "osgeo"})
# Optional third-party packages: a pure module that needs one is skipped (not
# failed) when it is missing locally. CI installs them.
OPTIONAL_ROOTS = frozenset({"numpy", "openpyxl"})


# --------------------------------------------------------------------------
# Discovery and static classification (AST only: no imports)
# --------------------------------------------------------------------------

@dataclass
class TestFile:
    name: str
    path: Path
    style: str                  # "run_all", "collect" or "none"
    requires_qgis: object       # True / False / None (not declared)
    group: str                  # "core" or "lay"
    summary: str                # first docstring line, may be ""
    toplevel_processing: bool   # imports the plugin's processing/ as top-level `processing`
    error: str = ""             # syntax error text, if the file does not parse

    __test__ = False            # not a pytest test class


def group_of(name: str, declared=None) -> str:
    if declared in GROUPS:
        return declared
    return "lay" if name.startswith(LAY_PREFIXES) else "core"


def _is_testcase_base(node: ast.expr) -> bool:
    text = ast.unparse(node) if hasattr(ast, "unparse") else ""
    return text.endswith("TestCase")


def _imports_toplevel_processing(tree: ast.Module) -> bool:
    """True when module-level code imports ``processing`` absolutely.

    Those modules (the MDB/.pthmdb worker suites) put the plugin folder on
    sys.path and import its ``processing/`` package as top-level
    ``processing`` - which would clash with QGIS's own Processing plugin.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "processing" or node.module.startswith("processing."):
                return True
        if isinstance(node, ast.Import):
            if any(a.name == "processing" or a.name.startswith("processing.") for a in node.names):
                return True
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                stack.append(child)
    return False


def inspect_test_file(path) -> TestFile:
    path = Path(path)
    name = path.stem
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except SyntaxError as exc:
        return TestFile(name, path, "none", None, group_of(name), "", False,
                        error=f"SyntaxError: {exc}")
    has_run_all = has_tests = False
    requires_qgis = declared_group = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "run_all":
                has_run_all = True
            elif node.name.startswith("test"):
                has_tests = True
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test") or any(_is_testcase_base(b) for b in node.bases):
                has_tests = True
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if not (isinstance(target, ast.Name) and isinstance(value, ast.Constant)):
                    continue
                if target.id == "REQUIRES_QGIS" and isinstance(value.value, bool):
                    requires_qgis = value.value
                elif target.id == "TEST_GROUP" and value.value in GROUPS:
                    declared_group = value.value
    style = "run_all" if has_run_all else ("collect" if has_tests else "none")
    doc = ast.get_docstring(tree) or ""
    summary = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return TestFile(name, path, style, requires_qgis, group_of(name, declared_group), summary,
                    _imports_toplevel_processing(tree))


def discover(names=None) -> list:
    """All ``tests/test_*.py`` files, sorted; or the named subset.

    Names may be given with or without the ``.py`` suffix. Unknown names
    raise ValueError so a typo never silently runs nothing.
    """
    available = {p.stem: p for p in sorted(TESTS_DIR.glob("test_*.py"))}
    if not names:
        return [inspect_test_file(p) for p in available.values()]
    chosen = []
    for raw in names:
        stem = Path(raw).stem if raw.endswith(".py") else raw
        if stem not in available:
            raise ValueError(f"no test module named {raw!r} in {TESTS_DIR}")
        chosen.append(inspect_test_file(available[stem]))
    return chosen


def select(files, *, skip=(), only=(), patterns=()):
    """Filter by group (``skip``/``only``) and ``-k`` substrings.

    A ``-k`` pattern matches the module name (underscores also read as
    spaces, so ``-k "planner store"`` still works) or its docstring summary.
    """
    chosen = []
    for info in files:
        if info.group in skip or (only and info.group not in only):
            continue
        if patterns:
            text = f"{info.name} {info.name.replace('_', ' ')} {info.summary}".lower()
            if not any(p.lower() in text for p in patterns):
                continue
        chosen.append(info)
    return chosen


# --------------------------------------------------------------------------
# Environment helpers
# --------------------------------------------------------------------------

def register_plugin_package() -> None:
    """Import the checkout as package ``subsea_cable_tools``.

    The plugin folder name does not matter (a git checkout is usually
    ``subsea-cable-tools``); tests use package-relative imports.
    """
    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load plugin package from {PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(PACKAGE_NAME, None)
        raise


def qgis_available() -> bool:
    try:
        return importlib.util.find_spec("qgis") is not None
    except (ImportError, ValueError):
        return False


_QGS_APP = None


def init_qgis() -> None:
    """Initialise a GUI-enabled (never shown) QgsApplication when standalone.

    Without ``initQgis()`` the ellipsoid/CRS registry is empty and every
    "ellipsoidal" measurement silently degrades to planar units. GUI=True is
    required because dialog/widget tests construct QWidgets. Inside a
    running QGIS the application already exists; nothing is done then.
    """
    global _QGS_APP
    from qgis.core import QgsApplication

    if QgsApplication.instance() is not None:
        return
    _QGS_APP = QgsApplication([], True)
    _QGS_APP.initQgis()


def reconfigure_stdio() -> None:
    """Never crash on a test name the console codepage cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="backslashreplace")


class QgisBlocker(importlib.abc.MetaPathFinder):
    """Make QGIS/Qt imports fail, so the pure runner classifies modules the
    same way whether it runs on plain Python or inside a QGIS Python.

    ``attempted`` records blocked imports and ``missing`` (filled by a
    companion finder at the end of sys.meta_path) records other top-level
    modules that could not be found - together they explain a failure of a
    module that imports QGIS or NumPy lazily inside its tests.
    """

    def __init__(self):
        self.attempted = set()
        self.missing = set()

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.partition(".")[0]
        if root in QGIS_ROOTS:
            self.attempted.add(root)
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (QGIS/Qt imports are blocked in the pure runner)",
                name=fullname)
        return None


class _MissingRecorder(importlib.abc.MetaPathFinder):
    """Last on sys.meta_path: only consulted for modules nobody can find."""

    def __init__(self, sink):
        self.sink = sink

    def find_spec(self, fullname, path=None, target=None):
        self.sink.add(fullname.partition(".")[0])
        return None


@contextlib.contextmanager
def qgis_blocked():
    blocker = QgisBlocker()
    recorder = _MissingRecorder(blocker.missing)
    sys.meta_path.insert(0, blocker)
    sys.meta_path.append(recorder)
    try:
        yield blocker
    finally:
        for finder in (blocker, recorder):
            with contextlib.suppress(ValueError):
                sys.meta_path.remove(finder)


def _exception_chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def missing_root(exc, roots) -> str:
    """The missing top-level module (from ``roots``) behind an import failure."""
    for link in _exception_chain(exc):
        if isinstance(link, ImportError):
            name = (link.name or "").partition(".")[0]
            if name in roots:
                return name
            match = re.search(r"No module named '([^'.]+)", str(link))
            if match and match.group(1) in roots:
                return match.group(1)
        if "PyQtGraph requires one of" in str(link) and roots is QGIS_ROOTS:
            return "PyQt"
    return ""


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


@contextlib.contextmanager
def isolated_imports(info: TestFile):
    """Contain the import side effects of one test module.

    Modules that import the plugin's ``processing/`` as top-level
    ``processing`` get the plugin folder on sys.path while they import and
    run, and any ``processing`` already imported (QGIS's Processing plugin in
    the smoke runner) is stashed and restored afterwards. sys.path entries a
    module adds for the plugin folder are removed again afterwards.
    """
    plugin_dir = _norm(PLUGIN_DIR)
    before = list(sys.path)
    stash = {}
    if info.toplevel_processing:
        for key in [k for k in sys.modules if k == "processing" or k.startswith("processing.")]:
            stash[key] = sys.modules.pop(key)
        sys.path.insert(0, str(PLUGIN_DIR))
    try:
        yield
    finally:
        before_norm = {_norm(p) for p in before if p}
        sys.path[:] = [p for p in sys.path
                       if not p or _norm(p) != plugin_dir or _norm(p) in before_norm]
        if info.toplevel_processing:
            for key in [k for k in sys.modules if k == "processing" or k.startswith("processing.")]:
                del sys.modules[key]
            sys.modules.update(stash)


def import_test_module(info: TestFile):
    register_plugin_package()
    return importlib.import_module(f"{PACKAGE_NAME}.tests.{info.name}")


# --------------------------------------------------------------------------
# Result interpretation
# --------------------------------------------------------------------------

def run_all_passed(result) -> bool:
    """Suites return per-test booleans, a failure count, or failure names."""
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, (list, tuple)) and all(isinstance(item, bool) for item in result):
        return all(result)
    if isinstance(result, int):
        return result == 0
    return not bool(result)


# --------------------------------------------------------------------------
# pytest stand-in
# --------------------------------------------------------------------------

class Skipped(Exception):
    """Raised by ``pytest.skip`` / ``importorskip`` in the stand-in."""


class Failed(AssertionError):
    """Raised by ``pytest.fail`` and a ``raises`` block that did not raise."""


Mark = namedtuple("Mark", "name args kwargs")


class MarkDecorator:
    def __init__(self, name, args=(), kwargs=None):
        self.name, self.args, self.kwargs = name, tuple(args), dict(kwargs or {})

    @property
    def mark(self):
        return Mark(self.name, self.args, self.kwargs)

    def __call__(self, *args, **kwargs):
        # Same rule as pytest: a single class/function argument (and no
        # kwargs) applies the mark; anything else adds mark arguments.
        if (len(args) == 1 and not kwargs
                and (inspect.isclass(args[0]) or inspect.isfunction(args[0]))):
            target = args[0]
            marks = list(getattr(target, "pytestmark", []))
            marks.append(self.mark)
            target.pytestmark = marks
            return target
        return MarkDecorator(self.name, self.args + args, {**self.kwargs, **kwargs})


class MarkGenerator:
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return MarkDecorator(name)


ParameterSet = namedtuple("ParameterSet", "values marks id")


def _param(*values, marks=(), id=None):  # noqa: A002 - mirrors pytest.param
    if isinstance(marks, MarkDecorator):
        marks = (marks,)
    return ParameterSet(tuple(values), tuple(m.mark if isinstance(m, MarkDecorator) else m
                                             for m in marks), id)


class ExceptionInfo:
    def __init__(self):
        self.type = self.value = self.tb = None

    def match(self, pattern):
        if not re.search(pattern, str(self.value)):
            raise AssertionError(f"Regex pattern {pattern!r} does not match {str(self.value)!r}")
        return True

    def errisinstance(self, exc):
        return isinstance(self.value, exc)

    def __repr__(self):
        return f"<ExceptionInfo {self.value!r}>"


class _RaisesContext:
    def __init__(self, expected, match=None):
        self.expected, self.match_expr = expected, match
        self.excinfo = ExceptionInfo()

    def __enter__(self):
        return self.excinfo

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise Failed(f"DID NOT RAISE {self.expected}")
        if not issubclass(exc_type, self.expected):
            return False
        self.excinfo.type, self.excinfo.value, self.excinfo.tb = exc_type, exc, tb
        if self.match_expr is not None:
            self.excinfo.match(self.match_expr)
        return True


def _raises(expected, *args, match=None, **kwargs):
    if not args:
        return _RaisesContext(expected, match)
    func, args = args[0], args[1:]
    with _RaisesContext(expected, match) as info:
        func(*args, **kwargs)
    return info


class _WarnsContext(contextlib.AbstractContextManager):
    def __init__(self, expected=Warning, match=None):
        self.expected, self.match_expr = expected, match
        self._catcher = warnings.catch_warnings(record=True)
        self.list = []

    def __enter__(self):
        self.list = self._catcher.__enter__()
        warnings.simplefilter("always")
        return self.list

    def __exit__(self, exc_type, exc, tb):
        self._catcher.__exit__(exc_type, exc, tb)
        if exc_type is not None:
            return False
        for item in self.list:
            if issubclass(item.category, self.expected) and (
                    self.match_expr is None or re.search(self.match_expr, str(item.message))):
                return False
        raise Failed(f"DID NOT WARN {self.expected} matching {self.match_expr!r}")


class _Approx:
    def __init__(self, expected, rel=None, abs=None, nan_ok=False):  # noqa: A002
        self.expected, self.rel, self.abs, self.nan_ok = expected, rel, abs, nan_ok

    def _close(self, actual, expected):
        if isinstance(expected, dict):
            return (isinstance(actual, dict) and actual.keys() == expected.keys()
                    and all(self._close(actual[k], expected[k]) for k in expected))
        if hasattr(expected, "tolist") and not isinstance(expected, (int, float)):
            expected = expected.tolist()
        if hasattr(actual, "tolist") and not isinstance(actual, (int, float)):
            actual = actual.tolist()
        if isinstance(expected, (list, tuple)):
            if isinstance(actual, (int, float)):
                return all(self._close(actual, e) for e in expected)
            try:
                actual = list(actual)
            except TypeError:
                return False
            return len(actual) == len(expected) and all(
                self._close(a, e) for a, e in zip(actual, expected))
        if isinstance(actual, (list, tuple)):
            return all(self._close(a, expected) for a in actual)
        try:
            actual_f, expected_f = float(actual), float(expected)
        except (TypeError, ValueError):
            return actual == expected
        if math.isnan(expected_f) or math.isnan(actual_f):
            return self.nan_ok and math.isnan(expected_f) and math.isnan(actual_f)
        if actual_f == expected_f:
            return True
        if math.isinf(expected_f) or math.isinf(actual_f):
            return False
        if self.rel is None and self.abs is None:
            tol = max(1e-6 * abs(expected_f), 1e-12)
        else:
            tol = max((self.rel or 0.0) * abs(expected_f), self.abs or 0.0)
        return abs(actual_f - expected_f) <= tol

    def __eq__(self, actual):
        return self._close(actual, self.expected)

    def __ne__(self, actual):
        return not self == actual

    __hash__ = None

    def __repr__(self):
        return f"approx({self.expected!r}, rel={self.rel}, abs={self.abs})"


def _skip(reason="", *, allow_module_level=False, msg=None):
    raise Skipped(msg if msg is not None else reason)


def _fail(reason="", pytrace=True, msg=None):
    raise Failed(msg if msg is not None else reason)


def _importorskip(modname, minversion=None, reason=None):
    try:
        module = importlib.import_module(modname)
    except ImportError as exc:
        raise Skipped(reason or f"could not import {modname!r}: {exc}") from None
    if minversion is not None:
        version = getattr(module, "__version__", None)
        parts = lambda v: tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])  # noqa: E731
        if version is None or parts(version) < parts(minversion):
            raise Skipped(f"module {modname!r} has version {version}, required is {minversion}")
    return module


def _fixture(fixture_function=None, *, scope="function", params=None, autouse=False,
             ids=None, name=None):
    def decorate(func):
        func._harness_fixture = {"scope": scope, "autouse": autouse, "name": name or func.__name__,
                                 "params": params}
        return func
    if fixture_function is not None and callable(fixture_function):
        return decorate(fixture_function)
    return decorate


def make_fake_pytest():
    """A ``pytest`` module stand-in with the subset the harness supports."""
    module = types.ModuleType("pytest")
    module.__doc__ = "Built-in pytest stand-in from tests/_harness.py (pytest not installed)."
    module.__harness_stand_in__ = True
    module.mark = MarkGenerator()
    module.raises = _raises
    module.warns = lambda expected=Warning, *, match=None: _WarnsContext(expected, match)
    module.deprecated_call = lambda match=None: _WarnsContext((DeprecationWarning, PendingDeprecationWarning), match)
    module.approx = _Approx
    module.skip = _skip
    module.skip.Exception = Skipped
    module.fail = _fail
    module.fail.Exception = Failed
    module.importorskip = _importorskip
    module.fixture = _fixture
    module.param = _param
    module.ExceptionInfo = ExceptionInfo

    def __getattr__(name):
        raise AttributeError(
            f"pytest.{name} is not supported by the built-in harness (tests/_harness.py). "
            "Extend the harness or run the module with real pytest: `pytest tests/`.")
    module.__getattr__ = __getattr__
    return module


def install_fake_pytest() -> None:
    """Provide ``import pytest`` for pytest-style modules inside the runners.

    The stand-in is used even when real pytest happens to be installed, so a
    runner behaves identically everywhere. Under a real pytest session (the
    conftest path) pytest is already imported and nothing is replaced.
    """
    if "pytest" not in sys.modules:
        sys.modules["pytest"] = make_fake_pytest()


# ----- built-in fixtures ---------------------------------------------------

_NOTSET = object()


class MonkeyPatch:
    def __init__(self):
        self._setattr, self._setitem, self._env = [], [], []
        self._syspath = None
        self._cwd = None

    def setattr(self, target, name, value=_NOTSET, raising=True):  # noqa: A003
        if value is _NOTSET:
            if not isinstance(target, str):
                raise TypeError("use setattr(target, name, value) or setattr('mod.attr', value)")
            value = name
            modpath, _, attr = target.rpartition(".")
            target = _resolve_dotted(modpath)
            name = attr
        old = getattr(target, name, _NOTSET)
        if raising and old is _NOTSET:
            raise AttributeError(f"{target!r} has no attribute {name!r}")
        if inspect.isclass(target):
            old = target.__dict__.get(name, _NOTSET)
        self._setattr.append((target, name, old))
        setattr(target, name, value)

    def delattr(self, target, name=_NOTSET, raising=True):  # noqa: A003
        if name is _NOTSET:
            modpath, _, name = target.rpartition(".")
            target = _resolve_dotted(modpath)
        if not hasattr(target, name):
            if raising:
                raise AttributeError(name)
            return
        old = getattr(target, name)
        if inspect.isclass(target):
            old = target.__dict__.get(name, _NOTSET)
        self._setattr.append((target, name, old))
        delattr(target, name)

    def setitem(self, dic, name, value):
        self._setitem.append((dic, name, dic.get(name, _NOTSET)))
        dic[name] = value

    def delitem(self, dic, name, raising=True):
        if name not in dic:
            if raising:
                raise KeyError(name)
            return
        self._setitem.append((dic, name, dic.get(name, _NOTSET)))
        del dic[name]

    def setenv(self, name, value, prepend=None):
        value = str(value)
        if prepend and name in os.environ:
            value = value + prepend + os.environ[name]
        self._env.append((name, os.environ.get(name)))
        os.environ[name] = value

    def delenv(self, name, raising=True):
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        self._env.append((name, os.environ.get(name)))
        del os.environ[name]

    def syspath_prepend(self, path):
        if self._syspath is None:
            self._syspath = list(sys.path)
        sys.path.insert(0, str(path))
        importlib.invalidate_caches()

    def chdir(self, path):
        if self._cwd is None:
            self._cwd = os.getcwd()
        os.chdir(str(path))

    def undo(self):
        for target, name, old in reversed(self._setattr):
            if old is _NOTSET:
                with contextlib.suppress(AttributeError):
                    delattr(target, name)
            else:
                setattr(target, name, old)
        self._setattr.clear()
        for dic, name, old in reversed(self._setitem):
            if old is _NOTSET:
                dic.pop(name, None)
            else:
                dic[name] = old
        self._setitem.clear()
        for name, old in reversed(self._env):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        self._env.clear()
        if self._syspath is not None:
            sys.path[:] = self._syspath
            self._syspath = None
        if self._cwd is not None:
            os.chdir(self._cwd)
            self._cwd = None


def _resolve_dotted(dotted):
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(f"cannot resolve {dotted!r}")


CaptureResult = namedtuple("CaptureResult", "out err")


class CaptureFixture:
    def __init__(self):
        self._out, self._err = io.StringIO(), io.StringIO()
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = self._out, self._err

    def readouterr(self):
        result = CaptureResult(self._out.getvalue(), self._err.getvalue())
        for stream in (self._out, self._err):
            stream.seek(0)
            stream.truncate()
        return result

    def close(self):
        sys.stdout, sys.stderr = self._saved


class _Request:
    def __init__(self, module, func, param=_NOTSET):
        self.module, self.function, self.node = module, func, types.SimpleNamespace(name=func.__name__)
        if param is not _NOTSET:
            self.param = param
        self.config = types.SimpleNamespace(getoption=lambda *a, **k: None)
        self._finalizers = []

    def addfinalizer(self, func):
        self._finalizers.append(func)


def _new_tmp_path():
    return Path(tempfile.mkdtemp(prefix="sct_test_"))


def _remove_tree(path):
    gc.collect()  # release sqlite/zip handles before deleting on Windows
    shutil.rmtree(path, ignore_errors=True)


BUILTIN_FIXTURES = ("tmp_path", "monkeypatch", "capsys", "request", "tmp_path_factory")


class _FixtureRunner:
    """Resolves fixtures for one test call; module-scoped values are cached."""

    def __init__(self, module, module_cache, module_finalizers):
        self.module = module
        self.defs = {}
        for obj in vars(module).values():
            meta = getattr(obj, "_harness_fixture", None)
            if meta is not None and callable(obj):
                self.defs[meta["name"]] = (obj, meta)
        self.module_cache = module_cache
        self.module_finalizers = module_finalizers

    def autouse(self):
        return [name for name, (_f, meta) in self.defs.items() if meta["autouse"]]

    def call(self, func, params, extra_fixtures=()):
        cache, finalizers = {}, []
        try:
            for name in list(extra_fixtures) + self.autouse():
                self._resolve(name, func, cache, finalizers, ())
            kwargs = {}
            for pname, param in inspect.signature(func).parameters.items():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                if pname in params:
                    kwargs[pname] = params[pname]
                elif param.default is not param.empty:
                    continue
                else:
                    kwargs[pname] = self._resolve(pname, func, cache, finalizers, ())
            return func(**kwargs)
        finally:
            errors = []
            for fin in reversed(finalizers):
                try:
                    fin()
                except Exception as exc:  # teardown errors must not mask the test result
                    errors.append(exc)
            if errors:
                print(f"    [WARN] fixture teardown error: {errors[0]!r}")

    def _resolve(self, name, func, cache, finalizers, stack):
        if name in cache:
            return cache[name]
        if name in self.module_cache:
            return self.module_cache[name]
        if name in stack:
            raise RuntimeError(f"recursive fixture dependency: {' -> '.join(stack + (name,))}")
        if name in self.defs:
            fixture_func, meta = self.defs[name]
            if meta["params"] is not None:
                raise NotImplementedError(
                    f"fixture {name!r}: parametrized fixtures are not supported by the harness; "
                    "use @pytest.mark.parametrize on the test instead")
            module_scoped = meta["scope"] != "function"
            fins = self.module_finalizers if module_scoped else finalizers
            kwargs = {}
            for pname, param in inspect.signature(fixture_func).parameters.items():
                if pname == "request":
                    kwargs[pname] = _Request(self.module, func)
                elif param.default is param.empty:
                    kwargs[pname] = self._resolve(pname, func, cache, finalizers, stack + (name,))
            value = fixture_func(**kwargs)
            if inspect.isgenerator(value):
                gen = value
                value = next(gen)

                def _finish(gen=gen):
                    with contextlib.suppress(StopIteration):
                        next(gen)
                fins.append(_finish)
            (self.module_cache if module_scoped else cache)[name] = value
            return value
        if name == "tmp_path":
            value = _new_tmp_path()
            finalizers.append(lambda p=value: _remove_tree(p))
        elif name == "tmp_path_factory":
            made = []

            def mktemp(basename="tmp", numbered=True):
                path = Path(tempfile.mkdtemp(prefix=f"sct_{basename}_"))
                made.append(path)
                return path
            value = types.SimpleNamespace(mktemp=mktemp, getbasetemp=lambda: Path(tempfile.gettempdir()))
            finalizers.append(lambda: [_remove_tree(p) for p in made])
        elif name == "monkeypatch":
            value = MonkeyPatch()
            finalizers.append(value.undo)
        elif name == "capsys":
            value = CaptureFixture()
            finalizers.append(value.close)
        elif name == "request":
            value = _Request(self.module, func)
        else:
            known = sorted(set(BUILTIN_FIXTURES) | set(self.defs))
            raise LookupError(f"fixture {name!r} not found (harness provides: {', '.join(known)})")
        cache[name] = value
        return value


# ----- collection + execution of pytest-style / unittest modules ----------

def _marks(obj):
    return list(getattr(obj, "pytestmark", []) or [])


def _eval_condition(condition, module):
    if isinstance(condition, str):
        return bool(eval(condition, dict(vars(module))))  # noqa: S307 - pytest string conditions
    return bool(condition)


def _id_for(value, argname, index):
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
        return text if len(text) <= 40 else f"{argname}{index}"
    return f"{argname}{index}"


def _expand_parametrize(func):
    """[(test_id_suffix, params_dict, extra_marks)] for a test function."""
    cases = [("", {}, [])]
    for mark in reversed([m for m in _marks(func) if m.name == "parametrize"]):
        argnames, argvalues = mark.args[0], list(mark.args[1]) if len(mark.args) > 1 else list(
            mark.kwargs.get("argvalues", []))
        ids = mark.kwargs.get("ids") or (mark.args[2] if len(mark.args) > 2 else None)
        if isinstance(argnames, str):
            argnames = [a.strip() for a in argnames.split(",") if a.strip()]
        argnames = list(argnames)
        expanded = []
        for index, raw in enumerate(argvalues):
            marks, explicit_id = [], None
            if isinstance(raw, ParameterSet):
                values, marks, explicit_id = raw.values, list(raw.marks), raw.id
            elif len(argnames) == 1:
                values = (raw,)
            else:
                values = tuple(raw)
            if len(values) != len(argnames):
                raise ValueError(f"parametrize: {argnames} got {len(values)} values: {raw!r}")
            if explicit_id is None and ids is not None:
                explicit_id = ids(raw) if callable(ids) else ids[index]
            case_id = explicit_id if explicit_id is not None else "-".join(
                _id_for(v, n, index) for v, n in zip(values, argnames))
            expanded.append((str(case_id), dict(zip(argnames, values)), marks))
        if not expanded:
            expanded = [("NOTSET", None, [Mark("skip", (), {"reason": "empty parameter set"})])]
        cases = [
            ("-".join(x for x in (base_id, case_id) if x),
             None if params is None or base is None else {**base, **params},
             base_marks + marks)
            for (base_id, base, base_marks) in cases
            for (case_id, params, marks) in expanded
        ]
    return cases


def _pytest_style_tests(module):
    """Module-level test functions and Test* classes (non-unittest), in file order."""
    items = []
    for name, obj in vars(module).items():
        if name.startswith("test") and isinstance(obj, MarkDecorator):
            raise TypeError(f"{name} is a MarkDecorator, not a test - mark misapplied")
        if name.startswith("test") and inspect.isfunction(obj) and obj.__module__ == module.__name__:
            if getattr(obj, "_harness_fixture", None) is None:
                items.append((obj.__code__.co_firstlineno, name, None, obj))
        elif (name.startswith("Test") and inspect.isclass(obj) and obj.__module__ == module.__name__
              and not issubclass(obj, unittest.TestCase) and "__init__" not in vars(obj)):
            for attr, member in vars(obj).items():
                if attr.startswith("test") and inspect.isfunction(member):
                    items.append((member.__code__.co_firstlineno, f"{name}.{attr}", obj, member))
    items.sort(key=lambda item: item[0])
    return [(name, cls, func) for _line, name, cls, func in items]


def run_collected(module, info: TestFile, *, skip_qgis_tests=False) -> int:
    """Run unittest classes and pytest-style tests of one module; return failures."""
    failures = 0
    counts = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0}
    module_cache, module_finalizers = {}, []
    fixtures = _FixtureRunner(module, module_cache, module_finalizers)
    try:
        for test_name, cls, func in _pytest_style_tests(module):
            marks = _marks(func) + (_marks(cls) if cls is not None else []) + _marks(module)
            for case_id, params, case_marks in _expand_parametrize(func):
                label = f"{test_name}[{case_id}]" if case_id else test_name
                outcome, detail = _run_one(module, fixtures, cls, func, params,
                                           marks + case_marks, skip_qgis_tests)
                counts[outcome] += 1
                tag = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "xfailed": "XFAIL"}[outcome]
                print(f"[{tag}] {label}" + (f" - {detail}" if detail and outcome != "failed" else ""))
                if outcome == "failed":
                    failures += 1
                    print(detail)
    finally:
        for fin in reversed(module_finalizers):
            with contextlib.suppress(Exception):
                fin()

    suite = unittest.TestLoader().loadTestsFromModule(module)
    if suite.countTestCases():
        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
        bad = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
        failures += bad
        counts["failed"] += bad
        counts["passed"] += result.testsRun - bad - len(result.skipped)
        counts["skipped"] += len(result.skipped)
    total = sum(counts.values())
    if total == 0:
        print(f"[FAIL] {info.name}: no tests collected")
        return 1
    print(f"-- {info.name}: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    return failures


def _run_one(module, fixtures, cls, func, params, marks, skip_qgis_tests):
    xfail = None
    extra_fixtures = []
    for mark in marks:
        if mark.name == "skip":
            return "skipped", mark.kwargs.get("reason") or (mark.args[0] if mark.args else "skipped")
        if mark.name == "skipif":
            conditions = mark.args or (mark.kwargs.get("condition"),)
            if any(_eval_condition(c, module) for c in conditions):
                return "skipped", mark.kwargs.get("reason", "skipif condition true")
        if mark.name == "xfail" and xfail is None:
            conditions = mark.args or (mark.kwargs.get("condition", True),)
            if all(_eval_condition(c, module) for c in conditions):
                xfail = mark
        if mark.name == "usefixtures":
            extra_fixtures.extend(mark.args)
    if params is None:  # empty parametrize
        return "skipped", "empty parameter set"
    target = func
    if cls is not None:
        instance = cls()
        target = getattr(instance, func.__name__)
    try:
        result = fixtures.call(target, params, extra_fixtures)
        if result is False:
            raise AssertionError("test returned False")
    except Skipped as exc:
        return "skipped", str(exc)
    except unittest.SkipTest as exc:
        return "skipped", str(exc)
    except BaseException as exc:  # a test failure is data here, not an error
        if isinstance(exc, KeyboardInterrupt):
            raise
        if skip_qgis_tests and missing_root(exc, QGIS_ROOTS):
            return "skipped", "needs QGIS"
        if skip_qgis_tests and missing_root(exc, OPTIONAL_ROOTS):
            return "skipped", f"needs {missing_root(exc, OPTIONAL_ROOTS)}"
        if xfail is not None:
            raises = xfail.kwargs.get("raises")
            if raises is None or isinstance(exc, raises):
                return "xfailed", xfail.kwargs.get("reason", "")
        return "failed", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if xfail is not None and xfail.kwargs.get("strict"):
        return "failed", "[XPASS(strict)] " + xfail.kwargs.get("reason", "")
    return "passed", ""


# --------------------------------------------------------------------------
# Running one module (used by both runners)
# --------------------------------------------------------------------------

@dataclass
class ModuleOutcome:
    status: str        # "passed", "failed", "needs-qgis", "needs-dep", "skipped"
    detail: str = ""
    seconds: float = 0.0


def run_test_file(info: TestFile, *, pure: bool) -> ModuleOutcome:
    """Import and run one test module.

    ``pure=True`` (pure runner): QGIS/Qt imports are blocked. A module that
    needs them - at import time, or lazily inside a failing test run - is
    reported as ``needs-qgis`` (the smoke runner runs it) instead of failing,
    unless it declared ``REQUIRES_QGIS = False``. A module that fails for
    want of an optional dependency (NumPy) is reported as ``needs-dep``.
    """
    t0 = time.perf_counter()

    def done(status, detail=""):
        return ModuleOutcome(status, detail, time.perf_counter() - t0)

    if info.error:
        print(f"[ERROR] {info.name}: {info.error}")
        return done("failed", info.error)
    if info.style == "none":
        msg = "no run_all(), test_* functions or TestCase classes found - cannot classify"
        print(f"[ERROR] {info.name}: {msg}")
        return done("failed", msg)
    if pure and info.requires_qgis is True:
        return done("needs-qgis", "declares REQUIRES_QGIS = True")

    install_fake_pytest()
    may_defer_to_qgis = pure and info.requires_qgis is None
    with (qgis_blocked() if pure else contextlib.nullcontext()) as blocker, isolated_imports(info):

        def explain_failure(exc=None):
            """needs-qgis / needs-dep outcome for a pure-runner failure, or None."""
            if not pure:
                return None
            qgis = missing_root(exc, QGIS_ROOTS) if exc is not None else ""
            qgis = qgis or ", ".join(sorted(blocker.attempted))
            if qgis and may_defer_to_qgis:
                return done("needs-qgis", f"needs {qgis}")
            dep = missing_root(exc, OPTIONAL_ROOTS) if exc is not None else ""
            dep = dep or ", ".join(sorted(blocker.missing & OPTIONAL_ROOTS))
            if dep:
                print(f"[SKIP] {info.name}: optional dependency {dep} is not installed")
                return done("needs-dep", dep)
            return None

        try:
            module = import_test_module(info)
        except Skipped as exc:
            print(f"[SKIP] {info.name}: {exc}")
            return done("skipped", str(exc))
        except Exception as exc:
            explained = explain_failure(exc)
            if explained is not None:
                return explained
            traceback.print_exc()
            print(f"[ERROR] {info.name}: import failed: {exc!r}")
            return done("failed", f"import failed: {exc!r}")
        try:
            if info.style == "run_all":
                ok = run_all_passed(module.run_all())
            else:
                ok = run_collected(module, info, skip_qgis_tests=pure) == 0
        except Exception as exc:
            explained = explain_failure(exc)
            if explained is not None:
                return explained
            traceback.print_exc()
            print(f"[ERROR] {info.name}: {exc!r}")
            return done("failed", repr(exc))
        if not ok:
            explained = explain_failure()
            if explained is not None:
                return explained
    return done("passed" if ok else "failed")


def format_duration(seconds: float) -> str:
    return f"{seconds:.1f} s"

