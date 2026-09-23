# -*- coding: utf-8 -*-
"""Run QGIS-dependent smoke tests from a QGIS Python environment."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
import time
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "subsea_cable_tools"
EXPECTED_ALGORITHM_COUNT = 40


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


# Cable lay simulator / catenary suites. They are numerically heavy (most of
# the suite's run time), so `--fast` skips them for changes that do not touch
# catenary/ or the lay-simulator tools. Run them whenever those change.
LAY_MODULES = {
    "test_catenary_solver",
    "test_simple_catenary",
    "test_drape_solver",
    "test_catenary_v2_dialog",
    "test_v3_solver3d",
    "test_v3_steady_lay",
    "test_v3_timeline",
    "test_v3_vessel_geometry",
    "test_v3_qgis_adapters",
}
GROUPS = ("core", "lay")

# (label, module name under tests/ or a check function)
CHECKS = [
    ('distance round trip', 'test_distance_round_trip'),
    ('KP geo utilities', 'test_kp_geo_utils'),
    ('catenary solver (V2)', 'test_catenary_solver'),
    ('simple catenary (V1)', 'test_simple_catenary'),
    ('drape solver (multi-span)', 'test_drape_solver'),
    ('catenary V2 dialog (auto-drape)', 'test_catenary_v2_dialog'),
    ('lay simulator 3D solver (V3)', 'test_v3_solver3d'),
    ('lay simulator steady lay (V3)', 'test_v3_steady_lay'),
    ('lay simulator timeline (V3)', 'test_v3_timeline'),
    ('lay simulator vessel geometry (V3)', 'test_v3_vessel_geometry'),
    ('lay simulator QGIS adapters (V3)', 'test_v3_qgis_adapters'),
    ('seabed length algorithm', 'test_seabed_length'),
    ('depth profile dock (generate, plot, measure)', 'test_depth_profile_dock'),
    ('KP range highlighter from CSV', 'test_kp_range_csv'),
    ('invalid-geometry input tolerance', 'test_invalid_geometry_sources'),
    ('cable lay importers', 'test_cable_lay_importers'),
    ('cable lay management ops', 'test_cable_lay_manage'),
    ('cable lay project data file ops', 'test_cable_lay_gpkg_ops'),
    ('MDB import algorithm', 'test_mdb_import_algorithm'),
    ('workbench store', 'test_workbench_store'),
    ('workbench route lineage', 'test_workbench_lineage'),
    ('workbench RPL engine', 'test_rpl_engine'),
    ('RPL import core (pure)', 'test_rpl_import_core'),
    ('RPL revision comparison (pure)', 'test_rpl_compare'),
    ('RPL import commit service', 'test_rpl_import_commit'),
    ('RPL from route line (KML)', 'test_rpl_from_line'),
    ('Path file import (.pthmdb)', 'test_pthmdb_import'),
    ('MBES XYZ raster tools', 'test_mbes_xyz_tools'),
    ('shared slope math (pure)', 'test_slope_utils'),
    ('KP Mouse profile math (pure)', 'test_kp_profile_math'),
    ('workbench assembly + fit', 'test_fit'),
    ('workbench topology + V3 adapter', 'test_workbench_adapter'),
    ('workbench layer styling (pure)', 'test_workbench_layer_style'),
    ('workbench project layers + restore', 'test_workbench_project_layers'),
    ('workbench rules engine', 'test_rules_engine'),
    ('workbench rules inputs + migrate', 'test_rules_inputs'),
    ('burial planner events (pure)', 'test_burial_events'),
    ('burial planner generation (pure)', 'test_burial_generation'),
    ('burial planner IO + import scan (pure)', 'test_burial_io'),
    ('burial planner targets + analysis currency (pure)', 'test_burial_targets_state'),
    ('burial planner persistence, overlays, inputs picker', 'test_burial_persistence'),
    ('burial planner report (pure)', 'test_burial_report'),
    ('burial planner profile data (pure)', 'test_burial_profile_data'),
    ('burial planner ground model + KP re-reference (pure)', 'test_burial_ground'),
    ('burial planner tools registry (pure)', 'test_burial_tools'),
    ('burial planner installation paths (pure)', 'test_burial_paths'),
    ('burial planner installation path task', 'test_burial_path_task'),
    ('burial planner store', 'test_burial_store'),
    ('burial planner ground model store + widgets', 'test_burial_ground_store'),
    ('burial planner BAS register (pure)', 'test_burial_bas'),
    ('burial planner BAS store + spreadsheet tab', 'test_burial_bas_store'),
    ('burial planner acquisition + task', 'test_burial_task'),
    ('planner timeline', 'test_planner_timeline'),
    ('planner MS Project export', 'test_planner_msproject_export'),
    ('planner store', 'test_planner_store'),
    ('planner feature references', 'test_planner_feature_ref'),
    ('planner RPL import', 'test_planner_rpl_import'),
    ('planner task table', 'test_planner_task_table'),
    ('planner spatial task tools', 'test_planner_spatial_tasks'),
    ('planner standard tasks', 'test_planner_standard_tasks'),
    ('planner operation types', 'test_planner_operation_types'),
    ('planner reports', 'test_planner_reports'),
    ('cable lay QC engine', 'test_laydata_qc'),
    ('experimental toolbar dropdown', 'test_experimental_toolbar'),
    ('QGIS compatibility widgets', 'test_qgis_compat_widgets'),
    ('processing provider', _provider_loads),
    ('main plugin import', _plugin_imports),
]


def _group(target) -> str:
    return "lay" if target in LAY_MODULES else "core"


def _target_name(target) -> str:
    return target if isinstance(target, str) else target.__name__


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run the QGIS-dependent smoke tests (all checks by default).")
    parser.add_argument("--fast", action="store_true",
                        help="skip the slow 'lay' group (catenary + lay simulator V1/V2/V3)")
    parser.add_argument("--skip", action="append", default=[], choices=GROUPS, metavar="GROUP",
                        help="skip a group: core or lay (repeatable)")
    parser.add_argument("--only", action="append", default=[], choices=GROUPS, metavar="GROUP",
                        help="run only these groups (repeatable)")
    parser.add_argument("-k", dest="patterns", action="append", default=[], metavar="TEXT",
                        help="run checks whose label or module contains TEXT, case-insensitive (repeatable)")
    parser.add_argument("--list", action="store_true", help="list the selected checks and exit")
    args = parser.parse_args(argv)
    if args.fast:
        args.skip.append("lay")
    return args


def _selected(args):
    chosen = []
    for label, target in CHECKS:
        group = _group(target)
        if group in args.skip or (args.only and group not in args.only):
            continue
        text = f"{label} {_target_name(target)}".lower()
        if args.patterns and not any(p.lower() in text for p in args.patterns):
            continue
        chosen.append((label, target, group))
    return chosen


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    checks = _selected(args)
    if args.list:
        for label, target, group in checks:
            print(f"{group:5} {_target_name(target):40} {label}")
        print(f"\n{len(checks)}/{len(CHECKS)} checks selected")
        return 0
    if not checks:
        print("No checks match the selection.")
        return 1

    _require_qgis()
    _init_qgis()
    _register_plugin_package()

    failures: list[str] = []
    timings = []
    started = time.perf_counter()
    for label, target, _group_name in checks:
        print(f"\n== {label} ==")
        t0 = time.perf_counter()
        try:
            ok = _run_module(f"{PACKAGE_NAME}.tests.{target}") if isinstance(target, str) else target()
            if not ok:
                failures.append(label)
        except Exception as exc:
            print(f"[ERROR] {label}: {exc!r}")
            failures.append(label)
        elapsed = time.perf_counter() - t0
        timings.append((elapsed, label))
        print(f"-- {label}: {elapsed:.1f} s")

    skipped = len(CHECKS) - len(checks)
    print(f"\nRan {len(checks)} checks in {time.perf_counter() - started:.0f} s"
          + (f" ({skipped} not selected)" if skipped else ""))
    print("Slowest:")
    for elapsed, label in sorted(timings, reverse=True)[:8]:
        print(f"  {elapsed:7.1f} s  {label}")

    if failures:
        print("\nSmoke test failures:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    print("\nAll selected QGIS smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
