# -*- coding: utf-8 -*-
"""pytest integration: ``pytest tests/`` where pytest is installed.

The runners (``run_pure_tests.py`` / ``run_qgis_smoke_tests.py``) remain the
reference; this makes the same modules usable from pytest and IDEs:

* ``run_all()``-style modules are collected as one item each
  (``tests/test_x.py::run_all``); their own ``test_*`` helpers return
  booleans and are not collected individually.
* pytest-style and unittest modules are collected natively, but imported as
  ``subsea_cable_tools.tests.<name>`` so package-relative imports work from
  any checkout folder name.
* Without QGIS, a module (or test) that needs QGIS is skipped, not failed,
  and so is one that needs a missing optional dependency (NumPy). With a
  QGIS Python a GUI-enabled QgsApplication is initialised once.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_harness():
    path = Path(__file__).resolve().with_name("_harness.py")
    name = "subsea_cable_tools_test_harness"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_harness()
HAVE_QGIS = harness.qgis_available()


def pytest_configure(config):
    harness.register_plugin_package()
    if HAVE_QGIS:
        harness.init_qgis()


def _skip_reason(exc, blocker=None):
    """Why a failure should be a skip in this environment, or ''."""
    if not HAVE_QGIS:
        qgis = harness.missing_root(exc, harness.QGIS_ROOTS) if exc is not None else ""
        if not qgis and blocker is not None and blocker.attempted:
            qgis = ", ".join(sorted(blocker.attempted))
        if qgis:
            return f"needs QGIS ({qgis})"
    dep = harness.missing_root(exc, harness.OPTIONAL_ROOTS) if exc is not None else ""
    if not dep and blocker is not None:
        dep = ", ".join(sorted(blocker.missing & harness.OPTIONAL_ROOTS))
    return f"needs {dep}" if dep else ""


class RunAllFailed(AssertionError):
    pass


class RunAllItem(pytest.Item):
    def __init__(self, *, info, **kwargs):
        super().__init__(**kwargs)
        self.info = info

    def runtest(self):
        info = self.info
        if info.error or info.style == "none":
            raise RunAllFailed(f"{info.name}: {info.error or 'no run_all(), test functions or TestCase classes'}")
        if info.requires_qgis and not HAVE_QGIS:
            pytest.skip("declares REQUIRES_QGIS = True")
        blocking = harness.qgis_blocked() if not HAVE_QGIS else _null_blocker()
        with blocking as blocker, harness.isolated_imports(info):
            try:
                module = harness.import_test_module(info)
                result = module.run_all()
            except Exception as exc:
                reason = _skip_reason(exc, blocker) if info.requires_qgis is not False else ""
                if reason:
                    pytest.skip(reason)
                raise
            if not harness.run_all_passed(result):
                reason = _skip_reason(None, blocker) if info.requires_qgis is not False else ""
                if reason:
                    pytest.skip(reason)
                raise RunAllFailed(f"{info.name}.run_all() reported failures (see captured output): "
                                   f"{result!r}"[:500])

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, RunAllFailed):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, 0, f"{self.info.name}::run_all"


class _null_blocker:
    attempted = frozenset()
    missing = frozenset()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RunAllFile(pytest.File):
    def collect(self):
        yield RunAllItem.from_parent(self, name="run_all", info=harness.inspect_test_file(self.path))


class PluginModule(pytest.Module):
    """Native pytest collection, imported as ``subsea_cable_tools.tests.<name>``."""

    def _getobj(self):
        info = harness.inspect_test_file(self.path)
        if info.requires_qgis and not HAVE_QGIS:
            pytest.skip("declares REQUIRES_QGIS = True", allow_module_level=True)
        with harness.isolated_imports(info):
            try:
                return harness.import_test_module(info)
            except Exception as exc:
                reason = _skip_reason(exc) if info.requires_qgis is not False else ""
                if reason:
                    pytest.skip(reason, allow_module_level=True)
                raise


def pytest_pycollect_makemodule(module_path, parent):
    path = Path(module_path).resolve()
    if path.parent != harness.TESTS_DIR:
        return None
    info = harness.inspect_test_file(path)
    if info.style == "collect":
        return PluginModule.from_parent(parent, path=path)
    return RunAllFile.from_parent(parent, path=path)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed and call.excinfo is not None and not isinstance(item, RunAllItem):
        reason = _skip_reason(call.excinfo.value)
        if reason:
            report.outcome = "skipped"
            report.longrepr = (str(item.path), 0, f"Skipped: {reason}")
