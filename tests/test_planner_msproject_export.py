# -*- coding: utf-8 -*-
"""Standalone checks for MS Project clipboard TSV generation."""

from datetime import datetime, timedelta

from ..planner.msproject_export import build_msp_tsv
from ..planner.timeline_engine import TaskSpec, compute_schedule


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_exact_tsv():
    anchor = datetime(2026, 8, 14, 3, 20)
    specs = [
        TaskSpec("a", 0, "Mobilise\nport", "v", duration_hours=1.5),
        TaskSpec("b", 1, "Lay\tA", "v", duration_hours=2,
                 predecessor_task_id="a"),
    ]
    result = compute_schedule(anchor, specs)
    text = build_msp_tsv(result.tasks, {s.task_id: s for s in specs},
                         {"v": {"name": "Vessel\t1"}}, first_row=7)
    expected = (
        "Mobilise port\t1.5 ehrs\t14/08/2026 03:20\t14/08/2026 04:50\t\tVessel 1\n"
        "Lay A\t2 ehrs\t14/08/2026 04:50\t14/08/2026 06:50\t7\tVessel 1"
    )
    return _result("exact Entry-table TSV + offset + sanitisation", text == expected, text)


def test_non_elapsed_and_blank_fields():
    specs = [TaskSpec("a", 0, "A", duration_hours=0)]
    result = compute_schedule(datetime(2026, 1, 1), specs)
    text = build_msp_tsv(result.tasks, {"a": specs[0]}, elapsed=False)
    ok = text == "A\t0 hrs\t01/01/2026 00:00\t01/01/2026 00:00\t\t"
    return _result("plain hours + blank predecessor/resource", ok, text)


def test_advanced_dependency_export():
    specs = [
        TaskSpec("a", 0, "A", "v", duration_hours=1),
        TaskSpec("b", 1, "B", "v2", duration_hours=1,
                 predecessor_task_id="a", dependency_type="SS", lag_hours=2),
    ]
    result = compute_schedule(datetime(2026, 1, 1), specs)
    text = build_msp_tsv(result.tasks, {spec.task_id: spec for spec in specs})
    ok = text.splitlines()[1].split("\t")[4] == "1SS+2h"
    return _result("advanced MS Project predecessor syntax", ok, text)


def run_all():
    return [test_exact_tsv(), test_non_elapsed_and_blank_fields(),
            test_advanced_dependency_export()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
