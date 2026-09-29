# -*- coding: utf-8 -*-
"""Run the QGIS-free test suites under plain Python (NumPy optional but
recommended - it exercises the vectorised paths).

Usage:  python tests/run_pure_tests.py [--fast] [--only GROUP] [-k TEXT] [--list] [module ...]

Every ``tests/test_*.py`` module is discovered automatically (see
``tests/_harness.py``). Each is imported with QGIS/Qt blocked: modules whose
import needs QGIS are reported as QGIS-only and left to
``tests/run_qgis_smoke_tests.py`` (which runs every module). A module can
state its category explicitly with ``REQUIRES_QGIS = True`` / ``False``;
with ``False`` a QGIS-dependent import is a failure rather than a skip.

--fast skips the slow "lay" group (lay simulator test_v3_* and the catenary
solvers); use it for changes outside catenary/ and the lay-simulator tools.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from pathlib import Path


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
PLUGIN_DIR = harness.PLUGIN_DIR
PACKAGE_NAME = harness.PACKAGE_NAME


def _register_plugin_package() -> None:  # kept for scripts that import this runner
    harness.register_plugin_package()


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Run the QGIS-free test suites.")
    parser.add_argument("modules", nargs="*", help="modules to run (default: every tests/test_*.py)")
    parser.add_argument("--fast", action="store_true",
                        help="skip the slow 'lay' group (lay simulator test_v3_*, catenary solvers)")
    parser.add_argument("--skip", action="append", default=[], choices=harness.GROUPS, metavar="GROUP",
                        help="skip a group: core or lay (repeatable)")
    parser.add_argument("--only", action="append", default=[], choices=harness.GROUPS, metavar="GROUP",
                        help="run only these groups (repeatable)")
    parser.add_argument("-k", dest="patterns", action="append", default=[], metavar="TEXT",
                        help="run modules whose name or docstring contains TEXT (repeatable)")
    parser.add_argument("--list", action="store_true",
                        help="classify the selected modules (imports them, runs nothing) and exit")
    args = parser.parse_args(argv)
    if args.fast:
        args.skip.append("lay")
    return args


def _classify_only(files) -> int:
    """--list: import each module with QGIS blocked and report its category."""
    harness.install_fake_pytest()
    counts = {}
    for info in files:
        if info.error or info.style == "none":
            category = "ERROR"
        elif info.requires_qgis is True:
            category = "qgis"
        else:
            with harness.qgis_blocked(), harness.isolated_imports(info):
                try:
                    harness.import_test_module(info)
                    category = "pure"
                except Exception as exc:
                    if info.requires_qgis is None and harness.missing_root(exc, harness.QGIS_ROOTS):
                        category = "qgis"
                    elif harness.missing_root(exc, harness.OPTIONAL_ROOTS):
                        category = "needs-" + harness.missing_root(exc, harness.OPTIONAL_ROOTS)
                    else:
                        category = "ERROR"
        counts[category] = counts.get(category, 0) + 1
        print(f"{category:12} {info.group:5} {info.style:8} {info.name}")
    print("\n" + ", ".join(f"{n} {c}" for c, n in sorted(counts.items())))
    return 1 if "ERROR" in counts else 0


def main(argv=None) -> int:
    harness.reconfigure_stdio()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        files = harness.discover(args.modules)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2
    files = harness.select(files, skip=args.skip, only=args.only, patterns=args.patterns)
    if not files:
        print("No test modules match the selection.")
        return 1
    harness.register_plugin_package()
    if args.list:
        return _classify_only(files)

    failures, needs_qgis, needs_dep, timings = [], [], [], []
    started = time.perf_counter()
    for info in files:
        print(f"\n== {info.name} ==")
        outcome = harness.run_test_file(info, pure=True)
        if outcome.status == "needs-qgis":
            print(f"[QGIS] {info.name}: {outcome.detail} - runs in tests/run_qgis_smoke_tests.py")
            needs_qgis.append(info.name)
            continue
        if outcome.status == "needs-dep":
            needs_dep.append(f"{info.name} ({outcome.detail})")
        elif outcome.status == "failed":
            failures.append(info.name)
        timings.append((outcome.seconds, info.name))

    ran = len(timings) - len(needs_dep)
    print(f"\nRan {ran} pure modules in {time.perf_counter() - started:.0f} s; "
          f"{len(needs_qgis)} QGIS-only modules left to the smoke runner.")
    if needs_dep:
        print("Skipped (optional dependency missing):", ", ".join(needs_dep))
    print("Slowest:", ", ".join(f"{n} {t:.1f} s" for t, n in sorted(timings, reverse=True)[:5]))
    if ran == 0 and not failures:
        print("\nFAILURES: no pure module ran")
        return 1
    print("\nFAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
