# -*- coding: utf-8 -*-
"""Run QGIS-dependent smoke tests from a QGIS Python environment."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "subsea_cable_tools"
EXPECTED_ALGORITHM_COUNT = 39


def _require_qgis() -> None:
    try:
        import qgis.core  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Run this script with QGIS Python, for example from an OSGeo4W shell "
            "or qgis_process environment."
        ) from exc


_QGS_APP = None


def _init_qgis() -> None:
    """Initialise a headless QgsApplication when run standalone.

    Without ``initQgis()`` the ellipsoid/CRS registry is empty, so
    ``QgsDistanceArea.setEllipsoid('WGS84')`` silently fails and every
    "ellipsoidal" measurement degrades to planar units. That made the
    distance tests fail spuriously (and would mask real regressions).
    Inside a running QGIS the application already exists; do nothing then.
    """
    global _QGS_APP
    from qgis.core import QgsApplication

    if QgsApplication.instance() is not None:
        return
    # GUI-enabled so widget-level tests (the V2 dialog) can construct widgets;
    # nothing is ever shown and no event loop is started.
    _QGS_APP = QgsApplication([], True)
    _QGS_APP.initQgis()


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
    _init_qgis()
    _register_plugin_package()

    checks = [
        ("distance round trip", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_distance_round_trip")),
        ("KP geo utilities", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_kp_geo_utils")),
        ("catenary solver (V2)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_catenary_solver")),
        ("simple catenary (V1)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_simple_catenary")),
        ("drape solver (multi-span)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_drape_solver")),
        ("catenary V2 dialog (auto-drape)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_catenary_v2_dialog")),
        ("lay simulator 3D solver (V3)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_v3_solver3d")),
        ("lay simulator steady lay (V3)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_v3_steady_lay")),
        ("lay simulator timeline (V3)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_v3_timeline")),
        ("lay simulator vessel geometry (V3)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_v3_vessel_geometry")),
        ("lay simulator QGIS adapters (V3)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_v3_qgis_adapters")),
        ("seabed length algorithm", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_seabed_length")),
        ("cable lay importers", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_cable_lay_importers")),
        ("MDB import algorithm", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_mdb_import_algorithm")),
        ("workbench store", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_workbench_store")),
        ("workbench route lineage", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_workbench_lineage")),
        ("workbench RPL engine", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_rpl_engine")),
        ("RPL import core (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_rpl_import_core")),
        ("RPL import commit service", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_rpl_import_commit")),
        ("RPL from route line (KML)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_rpl_from_line")),
        ("workbench assembly + fit", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_fit")),
        ("workbench topology + V3 adapter", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_workbench_adapter")),
        ("workbench layer styling (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_workbench_layer_style")),
        ("workbench project layers + restore", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_workbench_project_layers")),
        ("workbench rules engine", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_rules_engine")),
        ("workbench rules inputs + migrate", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_rules_inputs")),
        ("burial planner events (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_events")),
        ("burial planner generation (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_generation")),
        ("burial planner IO + import scan (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_io")),
        ("burial planner report (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_report")),
        ("burial planner profile data (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_profile_data")),
        ("burial planner tools registry (pure)", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_tools")),
        ("burial planner store", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_store")),
        ("burial planner acquisition + task", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_burial_task")),
        ("planner timeline", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_timeline")),
        ("planner MS Project export", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_msproject_export")),
        ("planner store", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_store")),
        ("planner feature references", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_feature_ref")),
        ("planner RPL import", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_rpl_import")),
        ("planner task table", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_task_table")),
        ("planner spatial task tools", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_spatial_tasks")),
        ("planner standard tasks", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_standard_tasks")),
        ("planner operation types", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_operation_types")),
        ("planner reports", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_planner_reports")),
        ("cable lay QC engine", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_laydata_qc")),
        ("experimental toolbar dropdown", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_experimental_toolbar")),
        ("QGIS compatibility widgets", lambda: _run_module(f"{PACKAGE_NAME}.tests.test_qgis_compat_widgets")),
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
