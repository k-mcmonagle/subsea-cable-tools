# -*- coding: utf-8 -*-
"""Run the QGIS-free test suites under plain Python (NumPy optional but
recommended — it exercises the vectorised paths).

Usage:  python tests/run_pure_tests.py [--fast] [-k TEXT] [module ...]

--fast skips the lay simulator (test_v3_*) suites, which dominate the run
time; use it for changes outside catenary/ and the lay-simulator tools.

Modules default to every suite that imports without the QGIS API. The
QGIS-dependent suites run via tests/run_qgis_smoke_tests.py instead.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "subsea_cable_tools"

PURE_MODULES = [
    "test_slope_utils",
    "test_profile_reliability",
    "test_kp_profile_math",
    "test_burial_profile_data",
    "test_burial_generation",
    "test_burial_events",
    "test_burial_io",
    "test_burial_report",
    "test_burial_ground",
    "test_burial_bas",
    "test_rules_engine",
    "test_planner_reports",
    "test_rpl_import_core",
    "test_rpl_compare",
    "test_system_topology",
    "test_burial_gpkg_sql",
    "test_burial_risk",
    "test_planner_task_import",
    "test_rules_engine_equivalence",
    "test_v3_assembly_datum",
    "test_v3_bu_full",
    "test_v3_bu_integration",
    "test_v3_bu_lowering_tool",
    "test_v3_bu_plan",
    "test_v3_integration_ui",
    "test_v3_manual",
    "test_v3_quick_bu",
    "test_v3_solver_regression",
    "test_v3_timeseries_view",
]


def _passed(result) -> bool:
    """Suites return either a list of per-test booleans or a failure count."""
    if isinstance(result, list):
        return all(result)
    return not bool(result)


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
    parser = argparse.ArgumentParser(description="Run the QGIS-free test suites.")
    parser.add_argument("modules", nargs="*", help="suites to run (default: all)")
    parser.add_argument("--fast", action="store_true", help="skip the lay simulator (test_v3_*) suites")
    parser.add_argument("-k", dest="patterns", action="append", default=[], metavar="TEXT",
                        help="run suites whose name contains TEXT (repeatable)")
    args = parser.parse_args()
    modules = args.modules or PURE_MODULES
    if args.fast:
        modules = [m for m in modules if not m.startswith("test_v3_")]
    if args.patterns:
        modules = [m for m in modules if any(p.lower() in m.lower() for p in args.patterns)]
    _register_plugin_package()
    failures = []
    timings = []
    for name in modules:
        print(f"\n== {name} ==")
        t0 = time.perf_counter()
        try:
            module = importlib.import_module(f"{PACKAGE_NAME}.tests.{name}")
            if not _passed(module.run_all()):
                failures.append(name)
        except Exception as exc:
            print(f"[ERROR] {name}: {exc!r}")
            failures.append(name)
        timings.append((time.perf_counter() - t0, name))
    print("\nSlowest:", ", ".join(f"{n} {t:.1f} s" for t, n in sorted(timings, reverse=True)[:5]))
    print("\nFAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
