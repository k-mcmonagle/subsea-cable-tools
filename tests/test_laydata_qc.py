# -*- coding: utf-8 -*-
"""Unit tests for the cable-lay QC engine (``laydata`` package).

These tests are pure Python + numpy (no QGIS), so they can run from the QGIS
Python console, the smoke runner, or a plain interpreter. Each test function
returns ``True`` / ``False`` and ``run_all`` prints PASS / FAIL per check.

Standalone example::

    import importlib.util, sys
    from pathlib import Path
    pkg_dir = Path(r'.../plugins/subsea-cable-tools')
    spec = importlib.util.spec_from_file_location(
        'subsea_cable_tools', pkg_dir / '__init__.py',
        submodule_search_locations=[str(pkg_dir)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules['subsea_cable_tools'] = pkg
    spec.loader.exec_module(pkg)
    from subsea_cable_tools.tests import test_laydata_qc
    test_laydata_qc.run_all()
"""

from __future__ import annotations

from typing import List

from ..laydata import LayDataset, QcRunner
from ..laydata.qc_checks import (
    DecimalPrecisionCheck,
    DistanceGapCheck,
    DuplicateCheck,
    TimeGapCheck,
)


def _iso(second: int) -> str:
    minute, sec = divmod(second, 60)
    hour, minute = divmod(minute, 60)
    return f"2024-01-05T{hour:02d}:{minute:02d}:{sec:02d}"


def _report(name: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    return ok


def test_time_gap_detects_single_gap() -> bool:
    # 1 s cadence, but jump from t=4 to t=30 (26 s gap) at index 4->5.
    seconds = [0, 1, 2, 3, 4, 30, 31, 32]
    columns = {
        "ISO_Time": [_iso(s) for s in seconds],
        "source_file": ["primary.csv"] * len(seconds),
    }
    lat = [10.0 + i * 1e-5 for i in range(len(seconds))]
    lon = [0.0] * len(seconds)
    ds = LayDataset(columns, lat=lat, lon=lon)
    findings = TimeGapCheck().run(ds, {"expected_interval_s": 1.0, "gap_factor": 1.5})
    ok = len(findings) == 1 and abs(findings[0].value - 26.0) < 1e-6
    ok = ok and findings[0].time_start == _iso(4) and findings[0].time_end == _iso(30)
    return _report("time gap detects single 26 s gap", ok)


def test_time_gap_auto_interval() -> bool:
    # 30 s cadence auto-detected; one 300 s gap should be flagged.
    seconds = [0, 30, 60, 90, 120, 420, 450]
    columns = {"ISO_Time": [_iso(s) for s in seconds], "source_file": ["a"] * len(seconds)}
    ds = LayDataset(columns)
    findings = TimeGapCheck().run(ds, {"expected_interval_s": 0.0, "gap_factor": 1.5})
    ok = len(findings) == 1 and abs(findings[0].value - 300.0) < 1e-6
    return _report("time gap auto-detects 30 s cadence", ok)


def test_time_gap_per_source_independent() -> bool:
    # Interleaved sources; each is internally continuous so no gaps expected.
    seconds = [0, 0, 1, 1, 2, 2]
    sources = ["a", "b", "a", "b", "a", "b"]
    columns = {"ISO_Time": [_iso(s) for s in seconds], "source_file": sources}
    ds = LayDataset(columns)
    findings = TimeGapCheck().run(ds, {"expected_interval_s": 1.0, "gap_factor": 1.5})
    return _report("time gap respects per-source grouping", len(findings) == 0)


def test_distance_gap() -> bool:
    # ~1.1 m steps then a ~111 m jump (0.001 deg lat) between idx 2 and 3.
    lat = [0.0, 0.00001, 0.00002, 0.00102, 0.00103]
    lon = [0.0] * 5
    columns = {
        "ISO_Time": [_iso(s) for s in range(5)],
        "source_file": ["a"] * 5,
    }
    ds = LayDataset(columns, lat=lat, lon=lon)
    findings = DistanceGapCheck().run(ds, {"max_spacing_m": 50.0})
    ok = len(findings) == 1 and findings[0].value > 100.0
    return _report("distance gap detects 111 m jump", ok)


def test_decimal_precision_flags_excess() -> bool:
    columns = {
        "ISO_Time": [_iso(s) for s in range(4)],
        "source_file": ["a"] * 4,
        "KP": ["1.234", "2.345", "3.4567", "4.560"],  # index 2 has 4 dp
    }
    ds = LayDataset(columns)
    findings = DecimalPrecisionCheck().run(ds, {"field": "KP", "expected_dp": 3})
    summary = findings[0]
    per_row = [f for f in findings[1:] if f.feature_fid == 2]
    ok = summary.value == 1.0 and len(per_row) == 1
    return _report("decimal precision flags one 4 dp value", ok)


def test_decimal_precision_all_ok() -> bool:
    columns = {
        "ISO_Time": [_iso(s) for s in range(3)],
        "source_file": ["a"] * 3,
        "KP": ["1.234", "2.500", "3.000"],
    }
    ds = LayDataset(columns)
    findings = DecimalPrecisionCheck().run(ds, {"field": "KP", "expected_dp": 3})
    ok = len(findings) == 1 and findings[0].value == 0.0 and findings[0].severity == "info"
    return _report("decimal precision passes clean 3 dp data", ok)


def test_duplicate_time() -> bool:
    seconds = [0, 1, 1, 2, 3, 3, 3]
    columns = {"ISO_Time": [_iso(s) for s in seconds], "source_file": ["a"] * len(seconds)}
    ds = LayDataset(columns)
    findings = DuplicateCheck().run(ds, {"mode": "time"})
    # one extra at t=1, two extras at t=3 -> 3 duplicate findings.
    return _report("duplicate check finds 3 duplicate rows", len(findings) == 3)


def test_runner_aggregates() -> bool:
    seconds = [0, 1, 2, 30]
    columns = {"ISO_Time": [_iso(s) for s in seconds], "source_file": ["a"] * 4}
    lat = [0.0, 0.00001, 0.00002, 0.00003]
    lon = [0.0] * 4
    ds = LayDataset(columns, lat=lat, lon=lon)
    runner = QcRunner(ds)
    findings = runner.run([
        (TimeGapCheck(), {"expected_interval_s": 1.0}),
        (DuplicateCheck(), {"mode": "time"}),
    ])
    rows = QcRunner.findings_to_rows(findings, "test_layer", "run1", _iso(0))
    ok = len(findings) == 1 and rows and rows[0]["src_layer"] == "test_layer"
    ok = ok and rows[0]["__wkt__"] is not None  # gap finding carried a position
    return _report("runner aggregates and builds rows", ok)


def run_all() -> List[bool]:
    results = [
        test_time_gap_detects_single_gap(),
        test_time_gap_auto_interval(),
        test_time_gap_per_source_independent(),
        test_distance_gap(),
        test_decimal_precision_flags_excess(),
        test_decimal_precision_all_ok(),
        test_duplicate_time(),
        test_runner_aggregates(),
    ]
    print(f"\n{sum(results)}/{len(results)} laydata QC checks passed.")
    return results


if __name__ == "__main__":
    run_all()
