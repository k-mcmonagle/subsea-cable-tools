# -*- coding: utf-8 -*-
"""Run the QGIS-free test suites under plain Python (NumPy optional but
recommended — it exercises the vectorised paths).

Usage:  python tests/run_pure_tests.py [module ...]

Modules default to every suite that imports without the QGIS API. The
QGIS-dependent suites run via tests/run_qgis_smoke_tests.py instead.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "subsea_cable_tools"

PURE_MODULES = [
    "test_slope_utils",
    "test_kp_profile_math",
    "test_burial_profile_data",
    "test_burial_generation",
    "test_burial_events",
    "test_burial_io",
    "test_burial_report",
    "test_rules_engine",
    "test_planner_reports",
    "test_rpl_import_core",
    "test_system_topology",
]


def _register_plugin_package() -> None:
    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load plugin package from {PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


def main() -> int:
    _register_plugin_package()
    modules = sys.argv[1:] or PURE_MODULES
    failures = []
    for name in modules:
        print(f"\n== {name} ==")
        try:
            module = importlib.import_module(f"{PACKAGE_NAME}.tests.{name}")
            results = module.run_all()
            if not all(results):
                failures.append(name)
        except Exception as exc:
            print(f"[ERROR] {name}: {exc!r}")
            failures.append(name)
    print("\nFAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
