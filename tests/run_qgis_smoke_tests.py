# -*- coding: utf-8 -*-
"""Run QGIS-dependent smoke tests from a QGIS Python environment."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "subsea_cable_tools"
EXPECTED_ALGORITHM_COUNT = 29


def _require_qgis() -> None:
    try:
        import qgis.core  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Run this script with QGIS Python, for example from an OSGeo4W shell "
            "or qgis_process environment."
        ) from exc


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


def _run_module(module_name: str) -> bool:
    module = importlib.import_module(module_name)
    result = module.run_all()
    if isinstance(result, list) and all(isinstance(item, bool) for item in result):
        return all(result)
    return not bool(result)


def _provider_loads() -> bool:
    provider_module = importlib.import_module(
        f"{PACKAGE_NAME}.processing.subsea_cable_processing_provider"
    )
    provider = provider_module.SubseaCableProcessingProvider()
    provider.loadAlgorithms()
    algorithms = list(provider.algorithms())
    names = sorted(algorithm.name() for algorithm in algorithms)
    ok = len(algorithms) >= EXPECTED_ALGORITHM_COUNT
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] processing provider registered {len(algorithms)} algorithms")
    if not ok:
        print("Registered algorithms:")
        for name in names:
            print(f"  {name}")
    return ok


def _plugin_imports() -> bool:
    module = importlib.import_module(f"{PACKAGE_NAME}.subsea_cable_tools")
    ok = hasattr(module, "SubseaCableTools")
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] main plugin module imports")
    return ok


def main() -> int:
    _require_qgis()
    _register_plugin_package()

    checks = [
        ("distance round trip", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_distance_round_trip")),
        ("KP geo utilities", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_kp_geo_utils")),
        ("processing provider", _provider_loads),
        ("main plugin import", _plugin_imports),
    ]

    failures: list[str] = []
    for label, check in checks:
        print(f"\n== {label} ==")
        try:
            if not check():
                failures.append(label)
        except Exception as exc:
            print(f"[ERROR] {label}: {exc!r}")
            failures.append(label)

    if failures:
        print("\nSmoke test failures:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print("\nAll QGIS smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
