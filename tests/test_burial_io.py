# -*- coding: utf-8 -*-
"""Checks for Burial Planner CSV IO + change-log inversion + import scan.

Pure python. The import scan asserts the ``burial`` package (and the shared
``kp_bars`` module) only imports qgis.PyQt / qgis.core / qgis.gui, NumPy,
vendored ``lib/`` packages, the stdlib and plugin-relative modules — the
spec's no-new-dependencies gate.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from ..burial import change_log, io_csv, schema

PLUGIN_DIR = Path(__file__).resolve().parents[1]

_ALLOWED_TOP_LEVEL = {
    "qgis", "numpy", "pyqtgraph",
    # vendored in lib/
    "openpyxl", "et_xmlfile", "access_parser", "construct", "tabulate",
}


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _plan():
    return {"plan_id": "p1", "name": "Test Plan", "method": "plough",
            "direction": 1, "scope_start_kp": 5.0, "scope_end_kp": 80.0,
            "rpl_name": "Route Rev 1", "rpl_id": "r1"}


def _event(kp, etype, status="candidate", locked=0, notes=""):
    return {"event_id": schema.new_id(), "plan_id": "p1", "generation_id": "",
            "seq": 0, "event_type": etype, "kp": kp, "end_kp": None,
            "lat": 50.0, "lon": -4.0, "depth_m": 123.4, "source": "auto",
            "status": status, "locked": locked, "notes": notes}


def test_events_csv_round_trip() -> bool:
    events = [_event(10.0, "BURIAL_START", status="confirmed", locked=1),
              _event(22.3456, "BURIAL_END", notes="grade out")]
    text = io_csv.events_csv(_plan(), events, "gen-1")
    ok = "# method: plough" in text and "# generation_id: gen-1" in text
    ok = ok and "PLDN" in text and "PLUP" in text
    ok = ok and "22.346" in text  # KP to 3 dp
    fmt, parsed = io_csv.detect_and_parse(text)
    ok = ok and fmt == "events_csv" and len(parsed) == 2
    ok = ok and parsed[0]["status"] == "confirmed" and parsed[0]["locked"] == 1
    ok = ok and abs(parsed[1]["kp"] - 22.346) < 1e-9
    return _result("events CSV round trip (metadata header, labels, 3 dp)", ok)


def test_kp_range_and_list_imports() -> bool:
    ranges = "start_kp,end_kp,note\r\n10.0,12.5,skip A\r\n20,21\r\n"
    fmt, events = io_csv.detect_and_parse(ranges, client_proposal=True)
    ok = fmt == "kp_ranges" and len(events) == 4
    ok = ok and all(e["source"] == "client_proposal" for e in events)
    ok = ok and events[0]["event_type"] == "BURIAL_START"
    listing = "kp,event_type\r\n5.0,PLDN\r\n6.0,PLUP\r\n"
    fmt2, events2 = io_csv.detect_and_parse(listing)
    ok = ok and fmt2 in ("events_list", "events_csv") and len(events2) == 2
    ok = ok and events2[0]["event_type"] == "BURIAL_START"
    bad = "kp,event_type\r\n5.0,NONSENSE\r\n"
    try:
        io_csv.parse_events_list_csv(bad)
        ok = False
    except io_csv.ImportError_:
        pass
    return _result("KP-range + events-list imports (client proposal tagging)", ok)


def test_change_log_inversion() -> bool:
    entry = change_log.make_entry(
        "p1", 3, change_log.ACTION_MOVE_EVENT, "e1",
        before={"bp_event": [{"event_id": "e1", "kp": 1.0}]},
        after={"bp_event": [{"event_id": "e1", "kp": 2.0},
                            {"event_id": "e2", "kp": 5.0}]},
        reason="nudge")
    ops = change_log.invert_entry(entry)
    ok = ("bp_event", "delete", ["e2"]) in ops
    upserts = [op for op in ops if op[1] == "upsert"]
    ok = ok and upserts and upserts[0][2][0]["kp"] == 1.0
    entries = [change_log.make_entry("p1", 0, "create_plan", "p1"),
               entry]
    ops2, undone = change_log.rollback_operations(entries, entry["change_id"])
    ok = ok and len(undone) == 1 and undone[0]["change_id"] == entry["change_id"]
    try:
        change_log.rollback_operations(entries, "missing")
        ok = False
    except ValueError:
        pass
    return _result("change-log entry inversion + rollback op ordering", ok)


def test_import_scan() -> bool:
    """No imports outside qgis/NumPy/vendored/stdlib/relative in burial/."""
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    if not stdlib:  # Python < 3.10 (QGIS 3.x): static fallback
        stdlib = {
            "__future__", "abc", "ast", "bisect", "collections", "copy", "csv",
            "dataclasses", "datetime", "enum", "functools", "getpass", "hashlib",
            "importlib", "io", "itertools", "json", "math", "os", "pathlib",
            "random", "re", "shutil", "string", "sys", "tempfile", "time",
            "traceback", "typing", "uuid", "warnings",
        }
    offenders = []
    targets = sorted((PLUGIN_DIR / "burial").rglob("*.py"))
    targets.append(PLUGIN_DIR / "workbench" / "kp_bars.py")
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # plugin-relative
                if node.module:
                    names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in _ALLOWED_TOP_LEVEL or top in stdlib or top == "sip":
                    continue
                offenders.append(f"{path.relative_to(PLUGIN_DIR)}: {name}")
    ok = not offenders
    return _result("import scan: no new dependencies in burial/",
                   ok, "; ".join(offenders[:5]))


def run_all() -> list:
    return [
        test_events_csv_round_trip(),
        test_kp_range_and_list_imports(),
        test_change_log_inversion(),
        test_import_scan(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
