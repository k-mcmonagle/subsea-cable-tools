# -*- coding: utf-8 -*-
"""Run every test module from a QGIS Python environment.

Usage:  <qgis python> tests/run_qgis_smoke_tests.py [--fast] [--only GROUP] [--skip GROUP]
                                                     [-k TEXT] [--list] [module ...]

Every ``tests/test_*.py`` module is discovered automatically and run - the
QGIS-dependent ones and the pure ones alike (see ``tests/_harness.py`` for
the supported module styles). Two extra checks follow: the Processing
provider registers every algorithm class under ``processing/*_algorithm.py``,
and the main plugin module imports.

--fast skips the slow "lay" group (catenary V1/V2, drape, lay simulator
test_v3_*). ``--list`` works on plain Python (no QGIS needed).

The checks run in a child process that this script supervises: if a module
hard-crashes the interpreter (access violation, abort in native code) it is
reported as CRASHED and the run resumes with the next module in a fresh
process. ``--in-process`` runs everything in this process instead.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
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
GROUPS = harness.GROUPS
LAY_PREFIXES = harness.LAY_PREFIXES


def _require_qgis() -> None:
    try:
        import qgis.core  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Run this script with QGIS Python, for example from an OSGeo4W shell "
            "or qgis_process environment."
        ) from exc


def _init_qgis() -> None:
    harness.init_qgis()


def _register_plugin_package() -> None:
    harness.register_plugin_package()


def _run_module(module_name: str) -> bool:
    """Run one test module by name (``test_x`` or ``subsea_cable_tools.tests.test_x``).

    Kept for ad-hoc subset scripts; the CLI accepts module names directly.
    """
    info = harness.discover([module_name.rpartition(".")[2]])[0]
    return harness.run_test_file(info, pure=False).status in ("passed", "skipped")


# --------------------------------------------------------------------------
# Extra checks (not test modules)
# --------------------------------------------------------------------------

def _algorithm_classes():
    """{class name: module} for every QgsProcessingAlgorithm subclass defined
    in processing/*_algorithm.py, plus import errors as [(module, traceback)]."""
    from qgis.core import QgsProcessingAlgorithm

    classes, errors = {}, []
    for path in sorted((PLUGIN_DIR / "processing").glob("*_algorithm.py")):
        module_name = f"{PACKAGE_NAME}.processing.{path.stem}"
        try:
            module = importlib.import_module(module_name)
        except Exception:
            errors.append((path.name, traceback.format_exc()))
            continue
        for name, obj in vars(module).items():
            if (inspect.isclass(obj) and obj.__module__ == module_name
                    and issubclass(obj, QgsProcessingAlgorithm)):
                classes[name] = path.name
    return classes, errors


def _provider_loads() -> bool:
    """Every algorithm class is registered, once, with a usable id.

    No hard-coded count: adding an algorithm file without registering it,
    or an algorithm that fails to import (the provider swallows the error so
    QGIS keeps loading), fails here. To retire an algorithm keep it
    registered and flag it Deprecated, or delete its file.
    """
    provider_module = importlib.import_module(
        f"{PACKAGE_NAME}.processing.subsea_cable_processing_provider")
    provider = provider_module.SubseaCableProcessingProvider()
    provider.loadAlgorithms()
    algorithms = list(provider.algorithms())
    problems = []

    expected, import_errors = _algorithm_classes()
    for filename, trace in import_errors:
        problems.append(f"processing/{filename} does not import:\n{trace}")
    registered = {type(alg).__name__ for alg in algorithms}
    for class_name, filename in sorted(expected.items()):
        if class_name not in registered:
            problems.append(f"{class_name} (processing/{filename}) is not registered by the provider")

    names = [alg.name() for alg in algorithms]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        problems.append(f"duplicate algorithm ids: {', '.join(duplicates)}")
    for alg in algorithms:
        if not alg.name() or not alg.displayName():
            problems.append(f"{type(alg).__name__} has an empty name() or displayName()")
    if not algorithms:
        problems.append("the provider registered no algorithms")

    ok = not problems
    print(f"[{'PASS' if ok else 'FAIL'}] processing provider registered {len(algorithms)} algorithms "
          f"({len(expected)} algorithm classes found under processing/*_algorithm.py)")
    for problem in problems:
        print(f"  {problem}")
    if not ok:
        print("Registered algorithms:")
        for name in sorted(names):
            print(f"  {name}")
    return ok


def _plugin_imports() -> bool:
    module = importlib.import_module(f"{PACKAGE_NAME}.subsea_cable_tools")
    ok = hasattr(module, "SubseaCableTools")
    print(f"[{'PASS' if ok else 'FAIL'}] main plugin module imports")
    return ok


# (label, function) - group "core"; they run after the test modules.
EXTRA_CHECKS = [
    ("processing provider", _provider_loads),
    ("main plugin import", _plugin_imports),
]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run every test module plus the provider/plugin checks under QGIS.")
    parser.add_argument("modules", nargs="*", help="test modules to run (default: every tests/test_*.py)")
    parser.add_argument("--fast", action="store_true",
                        help="skip the slow 'lay' group (catenary + lay simulator V1/V2/V3)")
    parser.add_argument("--skip", action="append", default=[], choices=GROUPS, metavar="GROUP",
                        help="skip a group: core or lay (repeatable)")
    parser.add_argument("--only", action="append", default=[], choices=GROUPS, metavar="GROUP",
                        help="run only these groups (repeatable)")
    parser.add_argument("-k", dest="patterns", action="append", default=[], metavar="TEXT",
                        help="run checks whose module name, docstring or label contains TEXT, "
                             "case-insensitive (repeatable)")
    parser.add_argument("--list", action="store_true", help="list the selected checks and exit")
    parser.add_argument("--in-process", action="store_true",
                        help="run in this process (no crash isolation: a hard crash ends the run)")
    # Internal, used by the supervisor for its child processes.
    parser.add_argument("--results-file", help=argparse.SUPPRESS)
    parser.add_argument("--extra", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--extras", action="store_true", help=argparse.SUPPRESS)  # = every extra check
    parser.add_argument("--extras-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.fast:
        args.skip.append("lay")
    return args


def _selected_extras(args):
    if args.extras:
        return list(EXTRA_CHECKS)
    if args.extra or args.extras_only:  # a supervised child: exactly the named checks
        return [(label, fn) for label, fn in EXTRA_CHECKS if fn.__name__ in args.extra]
    if args.modules or "core" in args.skip or (args.only and "core" not in args.only):
        return []
    if args.patterns:
        return [(label, fn) for label, fn in EXTRA_CHECKS
                if any(p.lower() in f"{label} {fn.__name__}".lower() for p in args.patterns)]
    return list(EXTRA_CHECKS)


def main(argv=None) -> int:
    harness.reconfigure_stdio()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        discovered = harness.discover(args.modules)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2
    files = harness.select(discovered, skip=args.skip, only=args.only, patterns=args.patterns)
    if args.extras_only:
        files = []
    extras = _selected_extras(args)
    total = len(discovered) + (0 if args.modules else len(EXTRA_CHECKS))
    if args.list:
        for info in files:
            print(f"{info.group:5} {info.style:8} {info.name:40} {info.summary[:70]}")
        for label, fn in extras:
            print(f"{'core':5} {'check':8} {fn.__name__:40} {label}")
        unclassified = [info.name for info in files if info.style == "none" or info.error]
        print(f"\n{len(files) + len(extras)}/{total} checks selected")
        if unclassified:
            print("Cannot classify (no run_all(), test functions or TestCase classes):",
                  ", ".join(unclassified))
            return 1
        return 0
    if not files and not extras:
        print("No checks match the selection.")
        return 1

    if not args.in_process:
        return _supervise(files, extras, total)
    # A hard crash (access violation, Qt abort) then prints the Python stack
    # of every thread before the process dies, so [CRASH] reports say where.
    import faulthandler
    faulthandler.enable(file=sys.__stderr__, all_threads=True)
    return _run_in_process(args, files, extras, total)


def _record(results_file, name, status, seconds):
    if results_file:
        with open(results_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "status": status, "seconds": seconds}) + "\n")


def _run_in_process(args, files, extras, total) -> int:
    _require_qgis()
    _init_qgis()
    _register_plugin_package()

    results = []
    started = time.perf_counter()
    for info in files:
        print(f"\n== {info.name} ==" + (f"  ({info.summary[:70]})" if info.summary else ""), flush=True)
        outcome = harness.run_test_file(info, pure=False)
        results.append((info.name, outcome.status, outcome.seconds))
        _record(args.results_file, info.name, outcome.status, outcome.seconds)
        print(f"-- {info.name}: {outcome.seconds:.1f} s", flush=True)
    for label, fn in extras:
        print(f"\n== {label} ==", flush=True)
        t0 = time.perf_counter()
        status = "passed"
        try:
            if not fn():
                status = "failed"
        except Exception as exc:
            traceback.print_exc()
            print(f"[ERROR] {label}: {exc!r}")
            status = "failed"
        elapsed = time.perf_counter() - t0
        results.append((label, status, elapsed))
        _record(args.results_file, label, status, elapsed)
        print(f"-- {label}: {elapsed:.1f} s", flush=True)
    if args.results_file:  # a supervisor prints the summary
        return 1 if any(status == "failed" for _n, status, _s in results) else 0
    return _summarise(results, total, time.perf_counter() - started)


def _supervise(files, extras, total) -> int:
    """Run the checks in child processes, resuming after a hard crash."""
    pending = [info.name for info in files]
    extra_labels = [label for label, _fn in extras]
    run_extras = bool(extras)
    results = []
    started = time.perf_counter()
    while pending or run_extras:
        handle, results_file = tempfile.mkstemp(prefix="sct_smoke_", suffix=".jsonl")
        os.close(handle)
        cmd = [sys.executable, "-u", str(Path(__file__).resolve()), "--in-process",
               "--results-file", results_file]
        if run_extras:
            cmd += [f"--extra={fn.__name__}" for _label, fn in extras]
            if not pending:
                cmd.append("--extras-only")
        cmd += pending
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            for raw in proc.stdout:
                sys.stdout.buffer.write(raw)
                sys.stdout.flush()
        except KeyboardInterrupt:
            proc.kill()
            raise
        code = proc.wait()
        done = {}
        with open(results_file, encoding="utf-8") as handle_in:
            for line in handle_in:
                row = json.loads(line)
                done[row["name"]] = row
        os.remove(results_file)
        results += [(row["name"], row["status"], row["seconds"]) for row in done.values()]
        if code in (0, 1):
            break
        # Hard crash: blame the first check that started but did not finish.
        undone = [name for name in pending if name not in done]
        undone_extras = [label for label in extra_labels if label not in done] if run_extras else []
        victim = (undone or undone_extras or ["(interpreter shutdown)"])[0]
        print(f"\n[CRASH] {victim}: the test process died with exit code {code} "
              f"({code & 0xFFFFFFFF:#x})" + ("; resuming with the next check" if undone[1:] or
                                               (undone and run_extras) else ""), flush=True)
        results.append((victim, "crashed", float("nan")))
        pending = undone[1:]
        run_extras = run_extras and bool(undone)
    return _summarise(results, total, time.perf_counter() - started)


def _summarise(results, total, elapsed) -> int:
    ran = len(results)
    not_selected = total - ran
    print(f"\nRan {ran} checks in {elapsed:.0f} s"
          + (f" ({not_selected} not selected)" if not_selected > 0 else ""))
    skipped = [name for name, status, _s in results if status == "skipped"]
    if skipped:
        print("Skipped at module level:", ", ".join(skipped))
    print("Slowest:")
    timed = [(seconds, name) for name, _st, seconds in results if seconds == seconds]  # drop NaN
    for seconds, name in sorted(timed, reverse=True)[:8]:
        print(f"  {seconds:7.1f} s  {name}")

    failures = [(name, status) for name, status, _s in results if status in ("failed", "crashed")]
    if failures:
        print("\nSmoke test failures:")
        for name, status in failures:
            print(f"  {name}" + (" (CRASHED)" if status == "crashed" else ""))
        return 1

    print("\nAll selected QGIS smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
