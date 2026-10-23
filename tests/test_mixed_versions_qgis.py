# -*- coding: utf-8 -*-
"""Sharing a project with users of another plugin version.

* Files from a newer version: the Burial Planner, Planner, Workbench and
  Span & Slack files stamped with a higher schema are refused before
  anything is written (no backup, no tables, stamp kept); Planner and
  Workbench writes to such a file are refused too.
* Cable Library: a save keeps the columns a newer version added.
* Bathymetry: a unit or vertical convention this version does not know
  reads as no data (never a wrong depth); a newer mosaic manifest is a
  plain error.
* Burial plan layers keep a newer version's style; a malformed cable-type
  mapping in the project reads as no mapping.
* Workbench rule editor: settings and choices it does not show survive OK.
* Route Comments dock: the comment layers stay in the project when this
  user cannot open the register (saved path from another computer, file
  gone, newer register) or keeps the map off; a register reachable only
  through the layer's own (project-relative) source is opened.

QSettings are redirected to a temporary folder.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import tempfile

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, QSettings

from .. import bathymetry_sampling as bs
from ..schema_guard import NewerVersionError

REQUIRES_QGIS = True


def _result(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def _stamp(path, table, value, key="schema_version"):
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(f'DELETE FROM "{table}" WHERE "key" = ?', (key,))
            conn.execute(f'INSERT INTO "{table}" ("key", "value") VALUES (?, ?)', (key, value))
    finally:
        conn.close()


def _meta(path, table):
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute(f'SELECT "key", "value" FROM "{table}"').fetchall())
    finally:
        conn.close()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    finally:
        conn.close()


def _drop_table(path, table):
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute("DELETE FROM gpkg_contents WHERE table_name = ?", (table,))
            conn.execute("DELETE FROM gpkg_geometry_columns WHERE table_name = ?", (table,))
    finally:
        conn.close()


def _raises(func, kind):
    try:
        func()
    except kind as exc:
        return str(exc)
    return ""


def test_newer_files_refused(folder):
    from ..burial import gpkg_sql
    from ..burial import schema as burial_schema
    from ..burial.store import BurialStore
    from ..planner.store import PlannerStore
    from ..span_analysis.store import SpanStore
    from ..workbench.store import WorkbenchNewerError, WorkbenchStore, WorkbenchStoreError

    checks = {}
    context = QgsProject.instance().transformContext()

    # Burial Planner: a newer plan file, missing a table this version would add.
    path = os.path.join(folder, "plans.gpkg")
    BurialStore(path, context).migrate()
    gpkg_sql.close(path)
    _stamp(path, "bp_meta", str(burial_schema.SCHEMA_VERSION + 1))
    _drop_table(path, "bp_vessel")
    message = _raises(lambda: BurialStore(path, context).migrate(), NewerVersionError)
    gpkg_sql.close(path)
    checks["burial refused"] = "newer version" in message and "plans.gpkg" in message
    checks["burial untouched"] = "bp_vessel" not in _tables(path) and \
        not glob.glob(os.path.join(folder, "plans.*.bak.gpkg")) and \
        _meta(path, "bp_meta")["schema_version"] == str(burial_schema.SCHEMA_VERSION + 1)
    _stamp(path, "bp_meta", str(burial_schema.SCHEMA_VERSION), key="min_compatible_version")
    try:
        BurialStore(path, context).migrate()
        checks["burial opens when the newer version allows it"] = \
            _meta(path, "bp_meta")["schema_version"] == str(burial_schema.SCHEMA_VERSION + 1)
    finally:
        gpkg_sql.close(path)

    # Planner: refused on open, and every write refused (whole-table rewrites).
    from ..planner import schema as planner_schema
    path = os.path.join(folder, "planner.gpkg")
    PlannerStore(path, context).migrate()
    _stamp(path, "pow_meta", str(planner_schema.SCHEMA_VERSION + 1))
    store = PlannerStore(path, context)
    checks["planner refused"] = "newer version" in _raises(store.migrate, NewerVersionError)
    checks["planner write refused"] = bool(_raises(
        lambda: store.create_scenario("S", "2026-01-01T00:00:00"), NewerVersionError)) and \
        not store.list_scenarios()

    # Workbench: refused on open and in every write transaction.
    from ..workbench import schema as wb_schema
    path = os.path.join(folder, "workbench.gpkg")
    WorkbenchStore(path, context).migrate()
    gpkg_sql.close(path)
    _stamp(path, wb_schema.TABLE_META, str(wb_schema.SCHEMA_VERSION + 1))
    store = WorkbenchStore(path, context)
    checks["workbench refused"] = "newer version" in _raises(store.migrate, WorkbenchNewerError)
    checks["workbench error is a store error"] = issubclass(WorkbenchNewerError, WorkbenchStoreError)
    checks["workbench write refused"] = bool(_raises(lambda: store.create_route("R"), WorkbenchNewerError)) \
        and not store.list_routes()
    gpkg_sql.close(path)

    # Span & Slack: refused on open.
    from ..span_analysis import store as span_store
    path = os.path.join(folder, "span.gpkg")
    SpanStore(path).ensure_created()
    gpkg_sql.close(path)
    _stamp(path, span_store.TABLE_META, str(span_store.SCHEMA_VERSION + 1))
    checks["span refused"] = "newer version" in _raises(SpanStore(path).ensure_created, NewerVersionError)
    gpkg_sql.close(path)

    failed = [k for k, v in checks.items() if not v]
    return _result("files from a newer plugin version are refused untouched", not failed, ", ".join(failed))


def test_cable_library_keeps_newer_columns(folder):
    from ..cable_library import store as lib

    path = os.path.join(folder, "library.gpkg")
    lib.create_library(path)
    lib.write_rows(path, [{"name": "LW-A", "category": "cable", "weight_water_kg_m": 1.0},
                          {"name": "SA-B", "category": "cable", "weight_water_kg_m": 2.0}])
    conn = sqlite3.connect(path)
    with conn:
        conn.execute('ALTER TABLE "cable_types" ADD COLUMN "torsion_knm" REAL')
        conn.execute('UPDATE "cable_types" SET "torsion_knm" = 7.5 WHERE "name" = ?', ("LW-A",))
    conn.close()
    rows = lib.read_rows(path)
    rows[0]["notes"] = "edited"
    rows.append({"name": "DA-C", "category": "cable"})
    lib.write_rows(path, rows, expected=lib.fingerprint(lib.read_rows(path)))
    conn = sqlite3.connect(path)
    kept = dict(conn.execute('SELECT "name", "torsion_knm" FROM "cable_types"').fetchall())
    notes = dict(conn.execute('SELECT "name", "notes" FROM "cable_types"').fetchall())
    conn.close()
    ok = kept == {"LW-A": 7.5, "SA-B": None, "DA-C": None} and notes["LW-A"] == "edited"
    return _result("cable library save keeps a newer version's columns", ok, repr((kept, notes)))


def test_bathymetry_conventions():
    checks = {
        "known units": abs(bs.normalise_depth(10.0, {"units": "ft", "vertical": "depth"}) - 3.048) < 1e-9,
        "defaults": bs.normalise_depth(10.0, {}) == 10.0,
        "unknown unit is no data": bs.normalise_depth(10.0, {"units": "cm", "vertical": "depth"}) is None,
        "unknown vertical is no data": bs.normalise_depth(10.0, {"units": "m", "vertical": "msl"}) is None,
    }
    record = {"path": "a.tif", "name": "a", "options": {}}
    checks["manifest read"] = bs._manifest_sources({"version": 2, "sources": [record]}, "m") == [record]
    checks["unversioned manifest read"] = bs._manifest_sources({"sources": [record]}, "m") == [record]
    for label, data in (("newer manifest", {"version": 3, "sources": [record]}),
                        ("manifest without sources", {"version": 2}),
                        ("manifest of the wrong shape", {"sources": {"path": "a.tif"}}),
                        ("manifest as a list", [record])):
        checks[label + " refused"] = bool(_raises(lambda d=data: bs._manifest_sources(d, "m"), ValueError))
    failed = [k for k, v in checks.items() if not v]
    return _result("bathymetry: unknown conventions are no data, newer manifests refused", not failed,
                   ", ".join(failed))


def test_project_side_guards():
    from ..burial import map_layers as bml
    from ..cable_library import store as lib

    layer = QgsVectorLayer("Point?crs=EPSG:4326", "plan layer", "memory")
    checks = {}
    for value, outdated in (("", True), ("0", True), ("1", False), ("2", False), ("x", True)):
        layer.setCustomProperty(bml._STYLE_VERSION_PROPERTY, value)
        checks[f"style {value or 'none'}"] = bml._style_outdated(layer) == outdated
    project = QgsProject.instance()
    try:
        for raw in ('["LW"]', '"LW"', '{"LW": {"name": "x"}, "SA": "Type SA"}'):
            project.writeEntry(lib.PROJECT_SCOPE, lib.PROJECT_MAPPING_KEY, raw)
            checks[f"mapping {raw}"] = lib.project_mapping() == ({"SA": "Type SA"} if raw.startswith("{") else {})
    finally:
        project.removeEntry(lib.PROJECT_SCOPE, lib.PROJECT_MAPPING_KEY)
    failed = [k for k, v in checks.items() if not v]
    return _result("newer layer styles kept; malformed cable-type mapping reads as none", not failed,
                   ", ".join(failed))


def test_rule_editor_keeps_unknown():
    from ..workbench import schema as wb_schema
    from ..workbench.assessment_panel import RuleEditorDialog

    rule = {"rule_id": "r1", "kind": wb_schema.RULE_KIND_THRESHOLD, "name": "Deep", "action": "monitor",
            "methods_json": json.dumps(["plough"]),
            "config_json": json.dumps({"profile": "tide", "op": ">", "value": 12.0, "future_key": [1, 2],
                                       "scope_ranges": [{"start_kp": 1.0, "end_kp": 2.0}]})}
    dialog = RuleEditorDialog(dict(rule), ["plough", "jetting"])
    try:
        result = dialog.result()
    finally:
        dialog.deleteLater()
    config = json.loads(result["config_json"])
    unknown_kind = RuleEditorDialog({"rule_id": "r2", "kind": "future_kind", "name": "F",
                                     "config_json": json.dumps({"a": 1})}, ["plough"])
    try:
        future = json.loads(unknown_kind.result()["config_json"])
    finally:
        unknown_kind.deleteLater()
    checks = {
        "unknown action kept": result["action"] == "monitor",
        "unknown profile kept": config.get("profile") == "tide",
        "unknown key kept": config.get("future_key") == [1, 2],
        "known values written": config.get("value") == 12.0 and config.get("op") == ">",
        "scope kept": config.get("scope_ranges") == [{"start_kp": 1.0, "end_kp": 2.0}],
        "unknown kind's settings kept": future == {"a": 1},
    }
    failed = [k for k, v in checks.items() if not v]
    return _result("workbench rule editor keeps what it does not show", not failed, ", ".join(failed))


class _Iface:
    def __init__(self):
        from qgis.gui import QgsMapCanvas, QgsMessageBar
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QMainWindow
        self.window = QMainWindow()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.canvas = QgsMapCanvas(self.window)
        self.window.setCentralWidget(self.canvas)
        self.bar = QgsMessageBar()

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def close(self):
        self.canvas.close()
        self.window.close()


def test_comment_layers_stay_in_the_project(folder):
    from ..route_comments import dock as dock_module
    from ..route_comments import map_layers, model
    from ..route_comments.store import CommentStore, close_connection

    project = QgsProject.instance()
    path = os.path.join(folder, "comments.gpkg")
    store = CommentStore.create(path)
    set_id = store.list_sets()[0]["set_id"]
    store.add_comment(set_id, {"text": "boulder", "ref_kind": model.REF_LOCATION, "wkt": "POINT(3 55)"})
    store.close()
    layers = map_layers.CommentLayers()
    layers.show(path, set_id, "Comments")
    # Saved by a colleague whose share has another drive letter.
    map_layers.set_project_register(os.path.join(folder, "Z_drive", "comments.gpkg"), set_id)
    qgz = os.path.join(folder, "project.qgz")
    project.write(qgz)
    project.clear()

    iface = _Iface()
    dock = dock_module.RouteCommentsDock(iface)
    checks = {}

    def layer_count():
        return len(map_layers.CommentLayers().layers())

    try:
        project.read(qgz)
        QCoreApplication.processEvents()
        checks["opened through the layer's source"] = dock.store is not None and \
            os.path.normcase(dock.store.path) == os.path.normcase(path) and len(dock.comments) == 1
        checks["layers kept"] = layer_count() == 2

        dock.act_show_map.setChecked(False)          # the user hides them: removed
        checks["hidden by the user: removed"] = layer_count() == 0
        dock.act_show_map.setChecked(True)
        project.write(qgz)
        QSettings().setValue(f"{dock_module.SETTINGS}/show_map", False)   # this user's own setting
        project.clear()
        project.read(qgz)
        checks["map off for this user: layers kept"] = layer_count() == 2
        QSettings().setValue(f"{dock_module.SETTINGS}/show_map", True)
        dock.act_show_map.blockSignals(True)
        dock.act_show_map.setChecked(True)
        dock.act_show_map.blockSignals(False)

        project.clear()
        close_connection()
        _stamp(path, "rc_meta", "2")              # a newer version's register
        project.read(qgz)
        checks["newer register: not opened, said so"] = dock.store is None and \
            "newer version" in dock.banner.text()
        checks["newer register: layers kept"] = layer_count() == 2
        checks["newer register: untouched"] = _meta(path, "rc_meta")["schema_version"] == "2"

        project.clear()
        close_connection()
        shutil.move(path, path + ".away")         # no access at all
        project.read(qgz)
        checks["register gone: not found, layers kept"] = dock.store is None and \
            "not found" in dock.banner.text() and layer_count() == 2
    finally:
        dock.shutdown()
        dock.deleteLater()
        map_layers.CommentLayers().remove()
        project.clear()
        map_layers.set_project_register("", "")
        close_connection()
        iface.close()
        QCoreApplication.processEvents()
    failed = [k for k, v in checks.items() if not v]
    return _result("comment layers stay in the project when this user cannot open the register", not failed,
                   ", ".join(failed))


def run_all():
    results = []
    old_format = QSettings.defaultFormat()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, temp)
        try:
            for test in (lambda: test_newer_files_refused(tempfile.mkdtemp(dir=temp)),
                         lambda: test_cable_library_keeps_newer_columns(tempfile.mkdtemp(dir=temp)),
                         test_bathymetry_conventions, test_project_side_guards, test_rule_editor_keeps_unknown,
                         lambda: test_comment_layers_stay_in_the_project(tempfile.mkdtemp(dir=temp))):
                try:
                    results.append(test())
                except Exception as exc:  # report, keep going
                    import traceback
                    traceback.print_exc()
                    results.append(_result(getattr(test, "__name__", "test"), False, repr(exc)))
        finally:
            QSettings.setDefaultFormat(old_format)
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
