# -*- coding: utf-8 -*-
"""Standalone checks for the MDB worker's pure-Python backend and dispatch.

Run from the plugin root (the worker deliberately has no package-relative
imports so it can execute as a bare subprocess script).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from processing import mdb_odbc_worker as worker


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def test_bundled_reader_importable():
    ok = worker.AccessParser is not None
    return _result("vendored access_parser importable from lib/", ok)


def test_gfeatures_interpretation():
    col_names = ["GeometryType", "Name", "PrimaryGeometryFieldName", "Description"]
    rows = [
        (1, "Bathy_Major", "LinearGeometry", ""),
        (33, "Description", "", ""),                   # text class, field resolved later
        (2, "Areas", "AreaGeometry", ""),
        ("bad", "Broken", "Geom", ""),                 # filtered: non-numeric type
        (1, None, "Geom", ""),                         # filtered: no name
    ]
    out = worker._feature_tables_from_gfeatures(col_names, rows)
    ok = out == {
        "Bathy_Major": {"geom_field_name": "LinearGeometry", "geometry_type_code": 1},
        "Description": {"geom_field_name": "", "geometry_type_code": 33},
        "Areas": {"geom_field_name": "AreaGeometry", "geometry_type_code": 2},
    }
    return _result("GFeatures rows filtered and mapped", ok, str(out))


def test_dispatch_prefers_pure_and_falls_back():
    calls = []

    def pure_ok():
        calls.append("pure")
        return {"t": 1}

    def pure_fail():
        calls.append("pure")
        raise RuntimeError("bad jet page")

    def odbc_ok():
        calls.append("odbc")
        return {"t": 2}

    ok = True
    calls.clear()
    ok = ok and worker._run_with_backends(pure_ok, odbc_ok) == {"t": 1} and calls == ["pure"]
    if worker.pyodbc is not None:
        calls.clear()
        ok = ok and worker._run_with_backends(pure_fail, odbc_ok) == {"t": 2} and calls == ["pure", "odbc"]
    return _result("dispatch: pure first, ODBC only on failure", ok, str(calls))


def test_dispatch_error_messages():
    saved_pyodbc = worker.pyodbc
    saved_parser = worker.AccessParser

    def fail(msg):
        def run():
            raise RuntimeError(msg)
        return run

    try:
        # Pure fails and no pyodbc: both facts surface.
        worker.pyodbc = None
        try:
            worker._run_with_backends(fail("bad jet page"), fail("unused"))
        except RuntimeError as exc:
            message = str(exc)
        ok = "bad jet page" in message and "ODBC fallback was unavailable" in message

        # Neither backend importable: actionable guidance.
        worker.AccessParser = None
        try:
            worker._run_with_backends(fail("unused"), fail("unused"))
        except RuntimeError as exc:
            message = str(exc)
        ok = ok and "reinstalling the plugin" in message and "pyodbc" in message
    finally:
        worker.pyodbc = saved_pyodbc
        worker.AccessParser = saved_parser
    return _result("dispatch error messages are actionable", ok)


def run_all():
    return [test_bundled_reader_importable(), test_gfeatures_interpretation(),
            test_dispatch_prefers_pure_and_falls_back(),
            test_dispatch_error_messages()]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
