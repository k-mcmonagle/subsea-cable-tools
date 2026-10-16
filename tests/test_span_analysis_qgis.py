# -*- coding: utf-8 -*-
"""Span & Slack Analysis in QGIS: the window, background runs, store and exports.

Synthetic data only: a 1 km line along the equator over a generated
sand-wave raster, and a synthetic seabed with a boulder.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from typing import List

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QCoreApplication

from ..cable_seabed import planning as pl
from ..span_analysis import dialog as dlg
from ..span_analysis import store as span_store
from ..span_analysis.setup_panel import CABLE_MANUAL
from .test_lay_assessment_qgis import DEG_PER_M, sand_wave_raster

REQUIRES_QGIS = True


class _Iface:
    def __init__(self):
        self._canvas = QgsMapCanvas()

    def mapCanvas(self):
        return self._canvas


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


def _wait(condition, timeout=90.0) -> bool:
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            return False
        QCoreApplication.processEvents()
        time.sleep(0.01)
    QCoreApplication.processEvents()
    return True


def _synthetic_settings():
    return {
        "route": {"source": "synthetic", "synthetic": {"length_m": 600.0, "spacing_m": 0.5, "start_kp": 0.0,
                                                       "depth_m": 100.0, "slope_deg": 0.0, "wave_height_m": 0.0,
                                                       "wave_length_m": 50.0, "roughness_m": 0.0,
                                                       "roughness_length_m": 5.0, "seed": 1}},
        "seabed": {"filter_m": 0.0, "features": [{"kind": "boulder", "kp": 0.3, "height": 1.0, "length": 2.0,
                                                  "crest": 0.0, "name": "B1"}]},
        "cable": {"source": CABLE_MANUAL, "manual": {"w_kg_m": 10.0 / 9.80665, "ei_knm2": 1.0, "mbr_m": 1.5,
                                                     "diameter_mm": 20.0, "npts_kn": 20.0},
                  "bodies": [{"name": "R1", "kind": "Repeater", "kp": 0.45, "length": 3.0, "weight": 3.0,
                              "diameter": 300.0}]},
        "control": {"mode": pl.TENSION, "value": 5.0, "direction": 1, "sections": [], "live": True},
        "criteria": {"allowable": True},
    }


def _make_dialog():
    dialog = dlg.SpanAnalysisDialog(None, iface=_Iface())
    dialog._persist_ui = False
    return dialog


def test_synthetic_run_live_detail_sweep() -> List[bool]:
    out = []
    dialog = _make_dialog()
    try:
        dialog.setup.apply_settings(_synthetic_settings())
        settings = dialog.setup.settings()
        dialog.setup.apply_settings(settings)
        out.append(_report("settings round trip", dialog.setup.settings() == settings))
        dialog.run()
        done = _wait(lambda: dialog._task is None)
        result = dialog._result
        ok = done and result is not None and len(result.spans) == 2 and len(result.bodies) == 1
        out.append(_report("background run: boulder spans and the repeater", ok,
                           f"{len(result.spans) if result else 0} span(s)"))
        if result is None:
            return out
        rows = dialog.results.spans_table.rowCount()
        out.append(_report("spans and findings tables filled", rows == 2 and
                           dialog.results.findings_table.rowCount() >= 2))
        before = sum(s.length_m for s in result.spans)
        dialog.setup.value.setValue(15.0)    # live: tension up, spans grow
        _wait(lambda: dialog._result is not result and dialog._task is None, 30.0)
        after = sum(s.length_m for s in dialog._result.spans) if dialog._result is not None else 0.0
        out.append(_report("live update on the tension value: spans grow", after > before * 1.3,
                           f"{before:.1f} m -> {after:.1f} m"))
        out.append(_report("previous run kept for the overlay", dialog._previous is result))
        dialog.results.spans_table.selectRow(0)
        dialog.run_detail(math.nan, math.nan, 0.0)
        _wait(lambda: dialog._task is None, 60.0)
        detail_rows = dialog.results.detail_table.rowCount()
        out.append(_report("detail model on the selected span", detail_rows >= 1,
                           dialog.results.detail_notes.text()[:120]))
        dialog.run_sweep({"tensions": [1.0, 5.0, 20.0], "window": "all"})
        _wait(lambda: dialog._task is None, 60.0)
        out.append(_report("tension sweep", dialog.results.sweep_table.rowCount() == 3))
        folder = tempfile.mkdtemp()
        for what in ("findings", "spans", "profile"):
            path = os.path.join(folder, f"{what}.csv")
            dlg.write_csv(dialog._result, what, path)
        with open(os.path.join(folder, "profile.csv"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        out.append(_report("CSV exports", len(lines) == dialog._result.stations.n + 1))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_raster_route_and_store() -> List[bool]:
    out = []
    folder = tempfile.mkdtemp()
    raster = QgsRasterLayer(sand_wave_raster(folder), "sand waves")
    line = QgsVectorLayer("LineString?crs=EPSG:4326", "route", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(0.0, 0.0), QgsPointXY(500.0 * DEG_PER_M, 0.0),
                                                    QgsPointXY(1000.0 * DEG_PER_M, 40.0 * DEG_PER_M * 0.25)]))
    line.dataProvider().addFeatures([feature])
    project = QgsProject.instance()
    project.addMapLayers([raster, line])
    gpkg = os.path.join(folder, "span_analysis", "test_span_analysis.gpkg")
    span_store.set_project_gpkg_path(gpkg)
    dialog = _make_dialog()
    try:
        settings = _synthetic_settings()
        settings["route"] = {"source": "line", "line_layer": line.id(), "line_selected": False, "start_kp": 10.0,
                             "kp_from": "", "kp_to": ""}
        settings["seabed"] = {"source": "raster", "rasters": [raster.id()], "interval": 0, "filter_m": 0.0,
                              "features": []}
        settings["cable"]["bodies"] = []
        settings["control"]["value"] = 3.0
        dialog.setup.refresh_sources()
        dialog.setup.apply_settings(settings)
        dialog.run()
        _wait(lambda: dialog._task is None, 120.0)
        result = dialog._result
        ok = result is not None and abs(float(result.stations.kp_km[0]) - 10.0) < 1e-6 and len(result.spans) > 5
        out.append(_report("line route over a raster: KP from the start KP, sand-wave spans", ok,
                           f"{len(result.spans) if result else 0} spans; {dialog.status.text()[:100]}"))
        turns = [f for f in (result.findings if result else []) if f.check_id == "alter_course"]
        out.append(_report("alter course found at the bend", len(turns) == 1 and abs(turns[0].kp_start - 10.5) < 0.01))
        lonlat = dialog.map.route.lonlat_at_kp(10.25) if dialog.map.route is not None else None
        out.append(_report("map link: KP to position", lonlat is not None and abs(lonlat[0] - 250.0 * DEG_PER_M) < 1e-6))
        original = dlg.QInputDialog.getText
        dlg.QInputDialog.getText = staticmethod(lambda *a, **k: ("Base case", True))
        try:
            saved = dialog.save_scenario()
        finally:
            dlg.QInputDialog.getText = original
        store = span_store.SpanStore(gpkg)
        rows = store.list_scenarios()
        profile = store.load_profile(rows[0]["scenario_id"]) if rows else None
        out.append(_report("scenario and sampled seabed saved in the project's GeoPackage",
                           saved and len(rows) == 1 and profile is not None and len(profile["x_m"]) > 100))
        dialog.add_to_map()
        layers = [layer for layer in project.mapLayers().values() if layer.name() == "Span analysis - Base case"]
        out.append(_report("findings layer added to the map", len(layers) == 1 and layers[0].featureCount() > 0))
        dialog.load_scenario(rows[0]["scenario_id"])
        out.append(_report("scenario reloads with its seabed (no new sampling needed)", dialog._profile is not None))
        for layer in layers:
            project.removeMapLayer(layer.id())
    finally:
        dialog.shutdown()
        dialog.deleteLater()
        project.removeMapLayers([raster.id(), line.id()])
        project.removeEntry(span_store.PROJECT_SCOPE, span_store.PROJECT_KEY_GPKG)
    return out


def run_all() -> List[bool]:
    results = test_synthetic_run_live_detail_sweep() + test_raster_route_and_store()
    print(f"\n{sum(results)}/{len(results)} Span & Slack Analysis QGIS checks passed.")
    return results
