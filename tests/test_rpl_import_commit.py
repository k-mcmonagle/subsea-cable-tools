# -*- coding: utf-8 -*-
"""QGIS-backed checks for the RPL import commit service.

Covers: neutral-model conversion (ChartNo/extras handling), projected CRS
transform, stated-vs-derived reconciliation, rollback-safe registration
(registry row last, staged artefacts removed on failure), revision lineage
beside an existing revision, and the durable wb_meta import audit.

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import os
import tempfile

from ..rpl_import import model as im
from ..rpl_import.model import ImportedRpl, ImportPoint, ImportProfile, ImportSegment
from ..workbench import schema
from ..workbench.rpl_import_service import (
    CommitError, CommitRequest, commit_import, make_wgs84_distance_area,
    read_import_audit, reconcile_model, to_rpl_model, transform_projected,
)
from ..workbench.store import WorkbenchStore


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" — " + detail) if detail else ""))
    return bool(ok)


def _temp_store() -> WorkbenchStore:
    # unique folder per test: OGR keeps GeoPackages open on Windows
    folder = tempfile.mkdtemp(prefix="rpl_import_commit_")
    store = WorkbenchStore(os.path.join(folder, "wb.gpkg"))
    store.migrate()
    return store


def _doc(n: int = 3) -> ImportedRpl:
    doc = ImportedRpl(sheet="RPL")
    for i in range(n):
        doc.points.append(ImportPoint(
            seq=i, source_row=5 + 2 * i, pos_no=i + 1,
            event=("BMH" if i == 0 else "AC%d" % i),
            lat=50.0 + 0.01 * i, lon=-1.0,
            dist_cum_km=1.112 * i, cable_dist_cum_km=1.134 * i,
            depth_m=10.0 * (i + 1), chart_no=("GB12%d" % i if i == 0 else str(i)),
            extras={"Zone": "UK"}))
    for i in range(n - 1):
        doc.segments.append(ImportSegment(
            seq=i, source_row=6 + 2 * i, dist_km=1.112, slack_pct=2.0,
            cable_dist_km=1.134, cable_type="LW", lay_vessel="V1",
            extras={"SegNote": "s%d" % i}))
    return doc


def test_to_rpl_model_chartno_and_extras() -> bool:
    model, diags = to_rpl_model(_doc(), source_file="src.xlsx")
    ok = len(model.points) == 3 and len(model.segments) == 2
    p0, p1 = model.points[0], model.points[1]
    ok = ok and p0.attrs.get("ChartNo") is None            # alphanumeric
    ok = ok and p0.attrs.get("ChartNoText") == "GB120"
    ok = ok and p1.attrs.get("ChartNo") == 1               # numeric stays int
    ok = ok and "ChartNoText" not in p1.attrs
    ok = ok and p0.attrs.get("Zone") == "UK"
    ok = ok and p0.attrs.get("SourceFile") == "src.xlsx"
    ok = ok and model.segments[0].attrs.get("CableType") == "LW"
    ok = ok and model.segments[0].attrs.get("SegNote") == "s0"
    ok = ok and any(d.rule_id == "rpl_import.point.chart_no_text" for d in diags)
    ok = ok and p0.dist_cum_km == 0.0 and p1.dist_cum_km == 1.112
    return _result("to_rpl_model: ChartNo canonical int + text evidence, extras", ok)


def test_reconcile_preserves_stated_and_fills_missing() -> bool:
    da = make_wgs84_distance_area()
    model, _ = to_rpl_model(_doc())
    stated = model.segments[0].dist_km
    model.segments[1].dist_km = None            # missing span -> derived
    model.segments[1].cable_dist_km = None
    model.points[2].dist_cum_km = None          # missing cumulative -> cascade
    report = reconcile_model(model, da, derive_missing=True)
    ok = model.segments[0].dist_km == stated            # stated untouched
    ok = ok and model.segments[1].dist_km is not None
    ok = ok and 1.0 < model.segments[1].dist_km < 1.3   # geodesic ~1.112
    ok = ok and model.segments[1].cable_dist_km is not None
    ok = ok and model.points[2].dist_cum_km is not None
    ok = ok and report.derived_dist == 1 and report.derived_cumulative
    ok = ok and report.derived_bearing >= 1
    return _result("reconcile: stated preserved, missing derived per segment", ok)


def test_transform_projected() -> bool:
    from qgis.core import QgsProject

    doc = ImportedRpl(sheet="P")
    # EPSG:32630 (UTM 30N): 500000E lies on the central meridian (3°W);
    # northing 5540407 transforms to lat ≈ 50.016° (verified with QGIS).
    doc.points.append(ImportPoint(seq=0, source_row=2,
                                  extras={"_easting": 500000.0,
                                          "_northing": 5540407.0}))
    doc.points.append(ImportPoint(seq=1, source_row=3,
                                  extras={"_easting": 500000.0,
                                          "_northing": 5551533.0}))
    profile = ImportProfile(coord_encoding=im.COORD_PROJECTED,
                            source_crs="EPSG:32630")
    diags = transform_projected(doc, profile,
                                QgsProject.instance().transformContext())
    ok = not diags
    ok = ok and doc.points[0].lat is not None
    ok = ok and abs(doc.points[0].lon + 3.0) < 1e-6      # central meridian
    ok = ok and abs(doc.points[0].lat - 50.016) < 0.01
    ok = ok and doc.points[1].lat > doc.points[0].lat

    bad = transform_projected(doc, ImportProfile(
        coord_encoding=im.COORD_PROJECTED, source_crs="EPSG:4326"),
        QgsProject.instance().transformContext())
    ok = ok and any(d.rule_id == "rpl_import.crs.not_projected" for d in bad)
    return _result("projected easting/northing transform via stated CRS", ok)


def test_commit_and_audit() -> bool:
    store = _temp_store()
    model, _ = to_rpl_model(_doc(), source_file="src.xlsx")
    reconcile_model(model, make_wgs84_distance_area())
    request = CommitRequest(route_name="S01", kind="planned",
                            audit={"sheet": "RPL", "parser_version": "test"})
    result = commit_import(store, model, request)
    row = store.get_rpl(result.rpl_id)
    ok = row is not None
    ok = ok and row.get("kind") == "planned"
    ok = ok and row.get("slack_mode") == "hold_slack"
    ok = ok and row.get("rev_label") == "Rev 1"
    ok = ok and row.get("status") == schema.STATUS_DRAFT
    ok = ok and row.get("route_id") == result.route_id

    points_layer = store.open_layer(result.points_layer)
    lines_layer = store.open_layer(result.lines_layer)
    ok = ok and points_layer is not None and lines_layer is not None
    if ok:
        points = sorted(points_layer.getFeatures(), key=lambda f: f["SeqNo"])
        lines = sorted(lines_layer.getFeatures(), key=lambda f: f["SeqNo"])
        ok = ok and len(points) == 3 and len(lines) == 2
        ok = ok and [f["SeqNo"] for f in points] == [0, 1, 2]
        ok = ok and lines[0]["FromPos"] == 1 and lines[0]["ToPos"] == 2
        names = [f.name() for f in points_layer.fields()]
        ok = ok and "Zone" in names and "ChartNoText" in names
        ok = ok and str(points[0]["ChartNoText"]) == "GB120"
        ok = ok and str(points[0]["Zone"]) == "UK"
        line_names = [f.name() for f in lines_layer.fields()]
        ok = ok and "SegNote" in line_names
        ok = ok and str(lines[1]["CableType"]) == "LW"

    component = store.component_for_subject(result.rpl_id)
    ok = ok and component is not None

    audit = read_import_audit(store, result.rpl_id)
    ok = ok and audit.get("sheet") == "RPL"
    ok = ok and audit.get("rev_label") == "Rev 1"
    ok = ok and audit.get("imported_utc")

    # second revision beside the first: label increments, supersedes set
    model2, _ = to_rpl_model(_doc(), source_file="src2.xlsx")
    reconcile_model(model2, make_wgs84_distance_area())
    result2 = commit_import(store, model2,
                            CommitRequest(route_name="s01", kind="as_laid"))
    row2 = store.get_rpl(result2.rpl_id)
    ok = ok and row2.get("rev_label") == "Rev 2"
    ok = ok and row2.get("supersedes_id") == result.rpl_id
    ok = ok and row2.get("slack_mode") == "hold_cable"
    ok = ok and row2.get("route_id") == result.route_id   # case-insensitive
    return _result("commit: registry row, layers, extras, lineage, audit", ok)


def test_duplicate_revision_blocked() -> bool:
    store = _temp_store()
    model, _ = to_rpl_model(_doc())
    reconcile_model(model, make_wgs84_distance_area())
    commit_import(store, model, CommitRequest(route_name="S02",
                                              rev_label="Rev 1"))
    layers_before = len(store.list_rpls())
    try:
        commit_import(store, model, CommitRequest(route_name="S02",
                                                  rev_label="Rev 1"))
        ok = False
    except CommitError:
        ok = True
    ok = ok and len(store.list_rpls()) == layers_before
    return _result("duplicate revision label refused before staging", ok)


class _FailingStore(WorkbenchStore):
    """save_rpl explodes after layers/meta/component were staged."""

    def save_rpl(self, row):
        raise RuntimeError("simulated write failure")


def test_failed_commit_leaves_no_artefacts() -> bool:
    folder = tempfile.mkdtemp(prefix="rpl_import_fail_")
    store = _FailingStore(os.path.join(folder, "wb.gpkg"))
    store.migrate()
    model, _ = to_rpl_model(_doc())
    reconcile_model(model, make_wgs84_distance_area())
    meta_before = dict(store.read_meta())
    routes_before = len(store.list_routes())
    components_before = len(store.list_components())
    try:
        commit_import(store, model, CommitRequest(route_name="S03"))
        ok = False
    except CommitError as exc:
        ok = "rolled back" in str(exc)
    clean = WorkbenchStore(store.gpkg_path)
    ok = ok and len(clean.list_rpls()) == 0
    ok = ok and len(clean.list_routes()) == routes_before
    ok = ok and len(clean.list_components()) == components_before
    ok = ok and clean.open_layer("rpl_S03_Rev_1_points") is None
    ok = ok and clean.open_layer("rpl_S03_Rev_1_lines") is None
    audit_keys = [k for k in clean.read_meta()
                  if k.startswith("import_audit_") and k not in meta_before]
    ok = ok and not audit_keys
    return _result("failed commit rolls back layers/meta/component/route", ok)


def test_inconsistent_model_refused() -> bool:
    from ..workbench.rpl_engine import RplModel, RplPoint

    store = _temp_store()
    lonely = RplModel(points=[RplPoint(seq=0, pos_no=1, event="", lat=0.0,
                                       lon=0.0)], segments=[])
    try:
        commit_import(store, lonely, CommitRequest(route_name="S04"))
        ok = False
    except CommitError:
        ok = True
    return _result("model with <2 points refused", ok)


def test_wizard_constructs() -> bool:
    """The guided wizard builds headlessly (catches Qt API/compat breakage)."""
    try:
        from qgis.PyQt.QtCore import QCoreApplication, QThread
        from qgis.PyQt.QtWidgets import QApplication

        from ..workbench import rpl_import_wizard as wizard_module
        from ..workbench.rpl_import_wizard import RplImportWizard, _GridModel

        wizard = RplImportWizard(store=None, iface=None)
        ok = len(wizard.pageIds()) == 3
        grid_model = _GridModel()
        from ..rpl_import.reader import SourceGrid

        grid = SourceGrid(
            sheet="T", rows=[[1, "a", "x"], [2, "b", "y"]], n_cols=3)
        grid_model.set_grid(grid, wizard.profile)
        ok = ok and grid_model.rowCount() == 2 and grid_model.columnCount() == 3
        index = grid_model.index(0, 1)
        ok = ok and grid_model.data(index) == "a"

        page = wizard.mapping_page
        wizard.grid = grid
        wizard.header_texts = ["pos", "event", "notes"]
        wizard.profile.mapping = {im.PF_POS_NO: 1, im.PF_EVENT: 2}
        wizard.profile.excluded_columns = [3]
        page.grid_model.set_grid(grid, wizard.profile)
        page._controls_to_profile_widgets(wizard.profile)
        ok = ok and page.mapping_table.columnCount() == 3
        ok = ok and page.mapping_table.cellWidget(0, 0).currentData() == im.PF_POS_NO
        ok = ok and page.mapping_table.cellWidget(0, 1).currentData() == im.PF_EVENT
        extra_combo = page.mapping_table.cellWidget(0, 2)
        ok = ok and extra_combo.currentData() == ""
        extra_combo.blockSignals(True)
        extra_combo.setCurrentIndex(extra_combo.findData("__include_as_extra__"))
        extra_combo.blockSignals(False)
        updated = page._profile_from_controls()
        ok = ok and updated.mapping == wizard.profile.mapping
        ok = ok and updated.excluded_columns == []
        ok = ok and page._profile_from_controls().mapping == wizard.profile.mapping
        ok = ok and page._crs_authid() == "EPSG:4326"
        ok = ok and page.flat_combo.isHidden()
        ok = ok and page.table.minimumHeight() >= 360
        ok = ok and page.mapping_table.horizontalHeader().isHidden()
        ok = ok and page.mapping_table.height() == 36

        scan_threads = []
        original_loader = wizard_module.ireader.load_sample_grids
        try:
            def fake_loader(_path):
                scan_threads.append(QThread.currentThread())
                return [SourceGrid(
                    sheet="RPL",
                    rows=[["Pos", "Lat (dd)", "Lon (dd)"],
                          [1, 50.0, -1.0], [2, 50.01, -1.0]],
                    n_cols=3)]

            wizard_module.ireader.load_sample_grids = fake_loader
            page1 = wizard.source_page
            page1._start_scan("example.xlsx")
            QApplication.processEvents()
            ok = ok and len(page1._results) == 1
            ok = ok and scan_threads == [QCoreApplication.instance().thread()]
        finally:
            wizard_module.ireader.load_sample_grids = original_loader
        wizard.deleteLater()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        return _result("import wizard constructs headlessly", False, str(exc))
    return _result("import wizard constructs headlessly", ok)


def run_all() -> list:
    return [
        test_wizard_constructs(),
        test_to_rpl_model_chartno_and_extras(),
        test_reconcile_preserves_stated_and_fills_missing(),
        test_transform_projected(),
        test_commit_and_audit(),
        test_duplicate_revision_blocked(),
        test_failed_commit_leaves_no_artefacts(),
        test_inconsistent_model_refused(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
