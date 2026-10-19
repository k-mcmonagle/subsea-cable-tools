# -*- coding: utf-8 -*-
"""Span & Slack Analysis in QGIS: the window, background runs, store and exports.

Synthetic data only: a 1 km line along the equator over a generated
sand-wave raster, and a synthetic seabed with a boulder.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import time
from typing import List

import numpy as np
from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QCoreApplication, QPoint, QPointF, Qt
from qgis.PyQt.QtTest import QTest

from ..cable_library import store as library
from ..cable_seabed import planning as pl
from ..span_analysis import dialog as dlg
from ..span_analysis import sources
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
    plot = dialog.plot
    plot.persist = False   # never write the tester's QSettings
    for check, on in ((plot.true_scale_check, False), (plot.reverse_check, False), (plot.sea_check, True),
                      (plot.labels_check, True)):
        check.setChecked(on)
    return dialog


def _example(title_start: str) -> int:
    from ..span_analysis.examples import EXAMPLES

    return next(i for i, (title, _tip, _builder) in enumerate(EXAMPLES) if title.startswith(title_start))


class _Point:
    def __init__(self, index):
        self._index = index

    def index(self):
        return self._index


def test_ui_tools() -> List[bool]:
    out = []
    dialog = _make_dialog()
    try:
        dialog.resize(1500, 950)
        dialog.show()
        dialog.load_example(_example("Verification"))
        _wait(lambda: dialog._task is None, 90.0)
        summary = dialog.results.summary.toHtml()
        out.append(_report("verification example: the Summary agrees with the closed form",
                           dialog._result is not None and "Agrees" in summary))
        plot = dialog.plot
        for _ in range(10):
            QCoreApplication.processEvents()
        plot.zoom_to(0.47, 0.53)
        for _ in range(10):
            QCoreApplication.processEvents()
        plot.measure.action.setChecked(True)
        widget = plot.widgets["depth"]
        vb = plot.depth_plot.vb
        for kp in (0.475, 0.525):
            view_pt = widget.mapFromScene(vb.mapViewToScene(QPointF(kp, 100.5)))
            QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(int(view_pt.x()), int(view_pt.y())))
            for _ in range(5):
                QCoreApplication.processEvents()
        out.append(_report("measure: two clicks on the depth plot make a measurement",
                           len(plot.measure.measurements) == 1))
        plot.measure.action.setChecked(False)
        plot.true_scale_check.setChecked(True)
        locked = plot.depth_plot.vb.state.get("aspectLocked")
        plot.true_scale_check.setChecked(False)
        plot.reverse_check.setChecked(True)
        inverted = plot.status_plot.vb.state.get("xInverted") and plot.slope_plot.vb.state.get("xInverted")
        plot.reverse_check.setChecked(False)
        out.append(_report("true scale locks the aspect; reverse KP inverts every row",
                           bool(locked) and bool(inverted) and not plot.depth_plot.vb.state.get("aspectLocked")))
        dialog.load_example(_example("Sand waves"))
        _wait(lambda: dialog._task is None, 120.0)
        table = dialog.results.spans_table
        table.sortItems(3, Qt.SortOrder.DescendingOrder)
        lengths = [float(table.item(r, 3).text()) for r in range(table.rowCount())]
        table.selectRow(0)
        chosen = dialog.results.selected_span()
        out.append(_report("spans sort by length (numbers, not text) and keep their span",
                           len(lengths) > 3 and lengths == sorted(lengths, reverse=True)
                           and chosen is not None and abs(chosen.length_m - lengths[0]) < 0.05))
        plot._on_body_clicked(None, [_Point(0)])
        rows = dialog.results.bodies_table.selectionModel().selectedRows()
        out.append(_report("clicking a body shows it in the Bodies tab", dialog.results.currentIndex() == 2
                           and len(rows) == 1 and "R1" in plot.readout.text()))
    finally:
        dialog.shutdown()
        dialog.hide()
        dialog.deleteLater()
    return out


class _Leg:
    def __init__(self, label):
        self.attrs = {"CableType": label}


class _Pos:
    def __init__(self, kp, lon=None, lat=0.0):
        self.dist_cum_km = kp
        self.lon = kp * 1000.0 * DEG_PER_M if lon is None else lon     # along the equator
        self.lat = lat


class _Model:
    def __init__(self, labels):
        self.points = [_Pos(float(k)) for k in range(len(labels) + 1)]
        self.segments = [_Leg(label) for label in labels]

    def start_kp_km(self):
        return self.points[0].dist_cum_km


def test_tables_and_dxf_route() -> List[bool]:
    import numpy as np

    from ..span_analysis import dxf_io

    out = []
    dialog = _make_dialog()
    try:
        table = dialog.setup.features
        rows = table.rows_from_records([["kind", "kp", "shape", "name"],
                                        ["Other (custom shape)", "0.25", "-3 0; 0 1; 3 0", "Mound"],
                                        ["boulder", "0.3", "", "B"]])
        positional = table.rows_from_records([["berm", "0.1", "1.2", "6", "2", "", "Mattress"]])
        out.append(_report("table paste / import: header by key or label, choices by label, or column order",
                           len(rows) == 2 and rows[0]["kind"] == "custom" and rows[0]["kp"] == 0.25
                           and rows[0]["shape"] == "-3 0; 0 1; 3 0" and rows[1]["kind"] == "boulder"
                           and positional[0]["kind"] == "berm" and positional[0]["crest"] == 2.0
                           and positional[0]["name"] == "Mattress"))
        table.set_rows([{"kind": "custom", "kp": 0.3, "shape": "-3 0; 0 x; 3 0"}])
        try:
            dialog.setup.features_list()
            bad = False
        except ValueError:
            bad = True
        table.set_rows([])
        out.append(_report("a custom shape that does not read is reported", bad))
        folder = tempfile.mkdtemp()
        x = np.arange(0.0, 400.0001, 1.0)
        z = -60.0 + np.where(np.abs(x - 200.0) < 20.0, 1.5 * np.cos((x - 200.0) / 20.0 * np.pi / 2.0), 0.0)
        writer = dxf_io.DxfWriter()
        writer.polyline(zip(x, z * 5.0), writer.layer("SEABED", 3))
        path = os.path.join(folder, "profile.dxf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(writer.document())
        settings = _synthetic_settings()
        settings["route"] = {"source": "dxf", "dxf": {"path": path, "layer": "SEABED", "x_mode": "chainage_m",
                                                      "start_kp": 5.0, "y_mode": "elevation", "ve": 5.0,
                                                      "offset": 0.0}}
        settings["seabed"] = {"filter_m": 0.0, "features": []}
        settings["cable"]["bodies"] = []
        dialog.setup.apply_settings(settings)
        dialog.run()
        _wait(lambda: dialog._task is None, 90.0)
        result = dialog._result
        ok = result is not None and abs(float(result.stations.kp_km[0]) - 5.0) < 1e-9 \
            and abs(float(-result.stations.seabed_m[0]) - 60.0) < 1e-6 and len(result.spans) >= 1
        out.append(_report("DXF drawing as the profile (chainage, elevation at 5x): KP, depth and spans", ok,
                           dialog.status.text()[:120]))
        scene = os.path.join(folder, "scene.dxf")
        dxf_io.export_scene(result, scene, dxf_io.SceneOptions(vertical_exaggeration=10.0))
        layer = QgsVectorLayer(scene, "scene", "ogr")
        names = {str(f["Layer"]) for f in layer.getFeatures()} if layer.isValid() else set()
        out.append(_report("scene DXF opens in QGIS (OGR) with its layers",
                           {"SA_SEABED", "SA_CABLE", "SA_SPAN_AMBER", "SA_TENSION_RESIDUAL"} <= names,
                           f"{len(names)} layers"))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_protection_and_find_tension() -> List[bool]:
    import numpy as np

    from ..span_analysis import dxf_io

    out = []
    dialog = _make_dialog()
    try:
        dialog.load_example(_example("Pipeline crossing with protection"))
        _wait(lambda: dialog._task is None, 90.0)
        result = dialog._result
        st = result.stations if result is not None else None
        drawn = dialog.plot.protection_curve.getData()[1]
        labels = [text for text, _kp, _depth in dialog.plot._labels()]
        settings = dialog.setup.settings()
        ok = st is not None and len(st.protection) == 1 and np.isfinite(drawn).sum() > 10             and "Articulated pipe" in labels and settings["cable"]["protection"][0]["weight"] == 45.0
        out.append(_report("protection: in the stations, drawn on the cable, labelled and saved", ok))
        folder = tempfile.mkdtemp()
        scene = os.path.join(folder, "protected.dxf")
        layers = dxf_io.export_scene(result, scene, dxf_io.SceneOptions(vertical_exaggeration=10.0))
        out.append(_report("scene DXF has a protection layer", "SA_PROTECTION" in dxf_io.read_polylines(scene)
                           and "SA_PROTECTION" in layers))
        results = dialog.results
        results.find_target.setCurrentIndex(results.find_target.findData("custom"))
        results.find_gap.setValue(0.3)
        results.find_length.setValue(6.0)
        results.find_mbr.setChecked(False)
        results._request_find()
        _wait(lambda: dialog._task is None, 120.0)
        answers = results._found
        found = answers[0].tension_kn if answers else 0.0
        out.append(_report("find tension runs in the background and fills its tab",
                           len(answers) == 1 and found > 0 and results.find_table.rowCount() == 1
                           and results.currentWidget() is results.find_page, f"{found:.2f} kN"))
        results.find_use.click()
        _wait(lambda: not dialog._live_timer.isActive() and dialog._task is None, 60.0)
        value = dialog.setup.value.value()
        applied = dialog.setup.mode.currentData() == pl.TENSION and value <= found + 1e-9 and found - value < 0.05
        lengths = [s.length_m for s in dialog._result.spans] if dialog._result is not None else []
        out.append(_report("'Use for the lay control' sets the tension; the spans then meet the target",
                           applied and all(v <= 6.0 + 1e-6 for v in lengths),
                           f"value {value:.2f} kN, longest {max(lengths) if lengths else 0:.2f} m"))
        result = dialog._result
        ends = sorted({round(s.kp_start, 4) for s in result.spans} | {round(s.kp_end, 4) for s in result.spans})
        kps = sorted(round(sp.kp, 4) for sp in result.supports)
        near = all(min(abs(e - k) for k in kps) < 0.0005 for e in ends)             and all(min(abs(k - e) for e in ends) < 0.0005 for k in kps)
        out.append(_report("span supports sit at the span ends (one crest support on the pipe, two touchdowns)",
                           len(kps) == 3 and near, f"{kps} vs {ends}"))
        crit = dialog.setup.crit
        crit["touchdown_gap_m"].setValue(0.02)
        crit["max_reaction_kn"].setValue(2.0)
        criteria = dialog.setup.criteria()
        saved = dialog.setup.settings()["criteria"]
        out.append(_report("touchdown gap and contact limit reach the criteria and the settings",
                           criteria.touchdown_gap_m == 0.02 and criteria.max_reaction_kn == 2.0
                           and saved["touchdown_gap_m"] == 0.02 and saved["max_reaction_kn"] == 2.0))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
    return out


class _Rpl:
    """A synthetic RPL: positions (lon, lat) with printed KPs, legs with slack and a cable type."""

    def __init__(self, lonlat, printed, start_kp):
        self.points = [_Pos(k, lon, lat) for (lon, lat), k in zip(lonlat, printed)]
        self.segments = [_Leg("LW") for _ in lonlat[1:]]
        for leg in self.segments:
            leg.slack_pct = 2.0

    def start_kp_km(self):
        return self.points[0].dist_cum_km


def test_kp_consistency() -> List[bool]:
    """KP on WGS84 routes far from the equator: windows, map picks, RPL legs and the hover marker."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

    from .. import depth_profile_core as dpc
    from ..kp_range_utils import make_distance_area

    out = []
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    utm = QgsCoordinateReferenceSystem("EPSG:32630")
    to_wgs = QgsCoordinateTransform(utm, wgs, QgsProject.instance())
    # Three legs of 25-30 km at about 57.5 N (alter courses between them).
    corners_utm = [QgsPointXY(500000.0, 6370000.0), QgsPointXY(520000.0, 6390000.0),
                   QgsPointXY(548000.0, 6380000.0), QgsPointXY(570000.0, 6395000.0)]
    corners = [to_wgs.transform(p) for p in corners_utm]
    planar = [12.345]
    for a, b in zip(corners_utm[:-1], corners_utm[1:]):
        planar.append(planar[-1] + math.hypot(b.x() - a.x(), b.y() - a.y()) / 1000.0)
    model = _Rpl([(p.x(), p.y()) for p in corners], planar, 12.345)
    route = sources.route_from_rpl_model(model, "test")
    vertices = sources.rpl_measured_kps(model)

    # Between positions the route follows the geodesic: on a UTM map it sits on the drawn leg
    # (stored in degrees it ran up to ~14 m off it mid-way along a 20 km leg at 57.5 N).
    to_utm = QgsCoordinateTransform(wgs, utm, QgsProject.instance())
    off = 0.0
    for i in range(3):
        a, b = corners_utm[i], corners_utm[i + 1]
        for f in (0.25, 0.5, 0.75):
            lon, lat = route.lonlat_at_kp(vertices[i] + f * (vertices[i + 1] - vertices[i]))
            q = to_utm.transform(QgsPointXY(lon, lat))
            dx, dy = b.x() - a.x(), b.y() - a.y()
            off = max(off, abs((q.x() - a.x()) * dy - (q.y() - a.y()) * dx) / math.hypot(dx, dy))
    turns = route.turns()
    out.append(_report("an RPL route follows the geodesic: on the leg drawn on a UTM map; KPs unchanged",
                       off < 1.0 and len(turns) == 2 and abs(route.kp_range[1] - vertices[-1]) < 1e-9
                       and all(abs(k - v) < 1e-9 for (k, _a), v in zip(turns, vertices[1:3])),
                       f"furthest {off:.3f} m from the drawn legs; turns at {[round(k, 3) for k, _a in turns]}"))

    # A KP window keeps the whole route's KPs (it used to be measured again: metres out).
    window = route.sub_route(20.0, 80.0)
    worst = 0.0
    for kp in (20.0, 25.0, vertices[1], 45.5, vertices[2], 79.9, 80.0):
        a, b = window.lonlat_at_kp(kp), route.lonlat_at_kp(kp)
        worst = max(worst, abs(a[0] - b[0]) * 111320.0 * math.cos(math.radians(a[1])), abs(a[1] - b[1]) * 111320.0)
    out.append(_report("a KP window keeps the route's KPs (WGS84, 57.5 N, 25-30 km legs)",
                       worst < 0.001 and abs(window.kp_range[1] - 80.0) < 1e-9, f"worst {worst * 1000:.3f} mm"))

    # Map pick: a point 200 m off a 2 km NE leg reads the KP of its perpendicular foot (as in UTM).
    a_utm, b_utm = QgsPointXY(500000.0, 6370000.0), QgsPointXY(501414.2, 6371414.2)
    query_utm = QgsPointXY(500707.1 - 141.42, 6370707.1 + 141.42)
    leg, _ = dpc.route_from_points([to_wgs.transform(a_utm), to_wgs.transform(b_utm)], wgs,
                                   make_distance_area(wgs))
    leg_utm, _ = dpc.route_from_points([a_utm, b_utm], utm, make_distance_area(utm))
    picked = sources.RouteGeo(leg, 0.0).kp_at(to_wgs.transform(query_utm), wgs)
    expect = sources.RouteGeo(leg_utm, 0.0).kp_at(query_utm, utm)
    out.append(_report("map pick on a WGS84 route matches the projected answer (200 m off, 57.5 N)",
                       picked is not None and expect is not None and abs(picked - expect) * 1000.0 < 1.0,
                       f"{picked * 1000:.1f} m vs {expect * 1000:.1f} m along" if picked and expect else ""))

    # RPL legs sit at the measured KPs of the positions; printed (grid) KPs are noted, not used.
    legs = sources.rpl_legs(model)
    mapper = sources.rpl_kp_mapper(model)
    note = sources.rpl_kp_note(model) or ""
    ok = len(legs) == 3 and all(abs(item[0] - vertices[i]) < 1e-6 and abs(item[1] - vertices[i + 1]) < 1e-6
                                for i, item in enumerate(legs))
    ok = ok and all(abs(mapper(planar[i]) - vertices[i]) < 1e-6 for i in range(4)) and "differ" in note
    out.append(_report("RPL legs and fits at measured KPs; printed KPs only noted", ok,
                       f"printed vs measured at the end {(planar[-1] - vertices[-1]) * 1000:+.1f} m"))
    # A start KP set by the user moves the route, its legs and the fit mapping together.
    moved = sources.with_start_kp(model, 10.0)
    moved_route = sources.route_from_rpl_model(moved, "moved")
    moved_legs = sources.rpl_legs(moved)
    shift = 10.0 - 12.345
    ok = abs(moved_route.kp_range[0] - 10.0) < 1e-12 and abs(moved_legs[0][0] - 10.0) < 1e-12         and abs(moved_legs[-1][1] - (vertices[-1] + shift)) < 1e-9         and abs(sources.rpl_kp_mapper(moved)(planar[1]) - (vertices[1] + shift)) < 1e-9
    out.append(_report("an RPL start KP set by the user moves the route, legs and fit mapping", ok))

    # A line in two parts with a 500 m gap: the gap is not chainage, in a window either.
    parts = [[to_wgs.transform(QgsPointXY(500000.0, 6370000.0)), to_wgs.transform(QgsPointXY(503000.0, 6370000.0))],
             [to_wgs.transform(QgsPointXY(503500.0, 6370000.0)), to_wgs.transform(QgsPointXY(506000.0, 6370000.0))]]
    distance = make_distance_area(wgs)
    length = sum(distance.measureLine(p[0], p[1]) for p in parts)
    two = sources.RouteGeo(dpc.RouteStationing(parts, length, wgs, distance), 0.0)
    cut = two.sub_route(2.0, 5.0)
    a, b = cut.lonlat_at_kp(4.0), two.lonlat_at_kp(4.0)
    gap_m = abs(a[0] - b[0]) * 111320.0 * math.cos(math.radians(a[1]))
    out.append(_report("a KP window over a gap between line parts does not count the gap", gap_m < 0.001
                       and len(cut.stationing.line_parts) == 2, f"{gap_m:.3f} m"))

    # Hover: the map marker goes to the station the readout shows; leaving the plot clears it.
    dialog = _make_dialog()
    try:
        dialog.setup.apply_settings(_synthetic_settings())
        dialog.run()
        _wait(lambda: dialog._task is None, 90.0)
        seen = []
        dialog.plot.kpHovered.connect(seen.append)
        snapped = dialog.plot.show_kp(0.30012)
        dialog.plot._end_hover()
        kp = dialog._result.stations.kp_km
        ok = snapped is not None and float(np.min(np.abs(kp - snapped))) == 0.0 and seen and math.isnan(seen[-1])
        out.append(_report("hover snaps to a station; leaving the plot clears the map marker", bool(ok)))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_rpl_cable_mapping() -> List[bool]:
    rows = [library.clean_row({"name": name, "category": "cable", "generic_type": generic,
                               "weight_water_kg_m": weight, "npts_kn": 50.0})
            for name, generic, weight in (("SA Alpha", "SA", 2.0), ("SA Beta", "SA", 3.0), ("LW Gamma", "LW", 0.5))]
    model = _Model(["LW", "SA", "SA"])
    try:
        sections, notes = sources.rpl_makeup(model, rows)
        ambiguous = len(sections) == 1 and sections[0].cable.name == "LW Gamma" and any("'SA'" in n for n in notes)
        sources.set_mapping("SA", "SA Beta")
        sections, notes = sources.rpl_makeup(model, rows)
        mapped = (len(sections) == 2 and sections[1].cable.name == "SA Beta"
                  and abs(sections[1].kp_start - 1.0) < 0.001 and abs(sections[1].kp_end - 3.0) < 0.001
                  and not notes)
    finally:
        sources.set_mapping("SA", None)
    return [_report("RPL cable types: an ambiguous 'SA' is reported, then resolves once mapped", ambiguous and mapped)]


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


def _utm_raster(folder: str) -> str:
    """A small flat GeoTIFF in UTM 31N (about 10°N 3°E), well away from the test route."""
    from osgeo import gdal, osr

    path = os.path.join(folder, "far_utm.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 50, 50, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((500000.0, 1.0, 0.0, 1100050.0, 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32631)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((50, 50), 500.0, dtype=np.float32))
    ds.FlushCache()
    ds = None
    return path


def _listed(dialog) -> dict:
    """Raster list of the setup: layer id -> name."""
    from ..qgis_compat import ITEM_DATA_USER_ROLE

    rl = dialog.setup.raster_list
    return {rl.item(i).data(ITEM_DATA_USER_ROLE): rl.item(i).text() for i in range(rl.count())}


def test_follows_project() -> List[bool]:
    """The window is kept when closed: it follows the project's layers, and another project's scenarios."""
    from qgis.PyQt.QtGui import QShowEvent
    from qgis.PyQt.QtXml import QDomDocument

    out = []
    folder = tempfile.mkdtemp()
    project = QgsProject.instance()
    gpkg_a = os.path.join(folder, "a", "span_analysis", "a_span_analysis.gpkg")
    gpkg_b = os.path.join(folder, "b", "span_analysis", "b_span_analysis.gpkg")
    span_store.set_project_gpkg_path(gpkg_a)
    near_path = sand_wave_raster(folder)
    line = QgsVectorLayer("LineString?crs=EPSG:4326", "route", "memory")
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(0.0, 0.0), QgsPointXY(1000.0 * DEG_PER_M, 0.0)]))
    line.dataProvider().addFeatures([feature])
    project.addMapLayer(line)
    dialog = _make_dialog()
    added = [line.id()]
    try:
        near = QgsRasterLayer(near_path, "MBES on the route")
        far = QgsRasterLayer(_utm_raster(folder), "MBES far away")
        project.addMapLayers([near, far])
        added += [near.id(), far.id()]
        ok = _wait(lambda: {near.id(), far.id()} <= set(_listed(dialog)), 5.0)
        out.append(_report("rasters added to the project appear in the open window", ok, str(_listed(dialog))))

        near.setName("MBES renamed")
        dialog.showEvent(QShowEvent())
        dialog.showEvent(QShowEvent())   # the second show: the window was closed and opened again
        ok = _wait(lambda: _listed(dialog).get(near.id()) == "MBES renamed", 5.0)
        out.append(_report("opening the window again reads the layers again (a renamed raster)", ok))

        setup = dialog.setup
        setup.apply_settings({"route": {"source": "line", "line_layer": line.id(), "start_kp": 0.0},
                              "seabed": {"source": "raster", "rasters": []}})
        setup.tick_rasters_along_route()
        out.append(_report("Along the route ticks only the raster the route crosses (one in UTM)",
                           setup.checked_raster_ids() == [near.id()],
                           f"{setup.checked_raster_ids()}; {setup.seabed_section.status.text()}"))

        dialog._dirty = False
        near_id, near_source = near.id(), near.source()
        project.removeMapLayer(near_id)
        ok = _wait(lambda: near_id not in _listed(dialog), 5.0)
        out.append(_report("a ticked raster removed from the project leaves the list and marks a change",
                           ok and setup.checked_raster_ids() == [] and dialog._dirty))

        again = QgsRasterLayer(near_path, "MBES added again")
        project.addMapLayer(again)
        added.append(again.id())
        _wait(lambda: again.id() in _listed(dialog), 5.0)
        setup.apply_settings({"seabed": {"source": "raster", "rasters": [near_id], "raster_sources": [near_source]}})
        out.append(_report("a scenario's raster added again (new layer id) is found by its file",
                           setup.checked_raster_ids() == [again.id()] and not setup._wanted))
        setup.apply_settings({"seabed": {"source": "raster", "rasters": [again.id(), "gone"],
                                         "raster_sources": [near_source, os.path.join(folder, "gone.tif")]}})
        kept = setup.settings()["seabed"]["rasters"]
        status = setup.seabed_section.status.text()
        out.append(_report("a scenario's raster not in the project is kept in the settings and named in the status",
                           kept == [again.id(), "gone"] and "not in the project" in status, f"{kept}; {status}"))

        settings_b = _synthetic_settings()
        store_b = span_store.SpanStore(gpkg_b)
        store_b.ensure_created()
        b_id = store_b.save_scenario("B case", settings_b, None, {})
        store_b.write_meta("last_scenario", b_id)
        store_b.close()
        dialog._dirty = False
        span_store.set_project_gpkg_path(gpkg_b)
        project.cleared.emit()
        project.readProject.emit(QDomDocument())
        ok = _wait(lambda: dialog._scenario_id == b_id, 5.0)
        out.append(_report("another project opened: its scenario file and last scenario load",
                           ok and dialog._store is not None and os.path.normcase(dialog._store.gpkg_path)
                           == os.path.normcase(gpkg_b) and dialog.scenario_combo.currentText() == "B case"
                           and not dialog._dirty, dialog.windowTitle()))

        dialog.setup.value.setValue(7.0)
        span_store.set_project_gpkg_path(gpkg_a)
        project.cleared.emit()
        project.addMapLayer(QgsRasterLayer(near_path, "loaded with the project"))   # layer events in between
        added.append(project.mapLayersByName("loaded with the project")[0].id())
        project.readProject.emit(QDomDocument())
        ok = _wait(lambda: not dialog._project_timer.isActive() and not dialog._layers_timer.isActive(), 5.0)
        out.append(_report("unsaved changes when a project opens: kept, unsaved, and said so",
                           ok and dialog._scenario_id is None and dialog._dirty
                           and abs(dialog.setup.value.value() - 7.0) < 1e-9 and "not saved" in dialog.status.text(),
                           dialog.status.text()[:120]))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
        project.removeMapLayers([i for i in added if project.mapLayer(i) is not None])
        project.removeEntry(span_store.PROJECT_SCOPE, span_store.PROJECT_KEY_GPKG)
    return out


def test_plot_and_layout_controls() -> List[bool]:
    """Plots menu, the A button, tooltip fields, the status bar, the view kept on Run, folds and tabs."""
    from ..cable_seabed.core import ERROR
    from ..span_analysis.plot_panel import DATA_ROWS

    out = []
    dialog = _make_dialog()
    try:
        dialog.load_example(_example("Sand waves"))
        _wait(lambda: dialog._task is None, 120.0)
        plot, results, setup = dialog.plot, dialog.results, dialog.setup
        first, last = float(dialog._result.stations.kp_km[0]), float(dialog._result.stations.kp_km[-1])

        plot.row_checks["slack"].setChecked(False)
        plot.row_checks["status"].setChecked(False)
        hidden = plot.rows["slack"].isHidden() and plot.rows["status"].isHidden()
        for key in DATA_ROWS:
            plot.row_checks[key].setChecked(False)
        profile_back = plot.row_checks["depth"].isChecked() and not plot.rows["depth"].isHidden()
        plot.default_view()
        all_back = all(action.isChecked() for action in plot.row_checks.values()) and not any(
            row.isHidden() for row in plot.rows.values())
        out.append(_report("Plots menu: rows hide and show, the profile stays when every plot is unticked, Default "
                           "view brings them back", hidden and profile_back and all_back))

        plot.depth_plot.setXRange(1.0, 1.1, padding=0)
        button = plot.depth_plot.autoBtn
        button.clicked.emit(button)
        lo, hi = plot.visible_kp()
        out.append(_report("the A button is on and fits the whole profile",
                           not plot.depth_plot.buttonsHidden and lo <= first + 1e-6 and hi >= last - 1e-6,
                           f"{lo:.3f} to {hi:.3f}"))

        st = dialog._result.stations
        i = int(np.argmin(np.abs(st.kp_km - 1.1)))
        for key, action in plot.tip_fields.items():
            action.setChecked(key in ("kp", "tension", "slope"))
        text = plot.tip_html(i, plot.tip_keys())
        out.append(_report("tooltip shows the chosen fields only", "KP" in text and "Residual T" in text
                           and "Slope" in text and "Seabed" not in text, text[:80]))
        pos = plot.depth_plot.vb.mapViewToScene(QPointF(1.1, float(-dialog._result.cable_m[i])))
        plot._show_tip("depth", pos, text)
        shown = not plot.tip.isHidden()
        plot._end_hover()
        out.append(_report("tooltip opens beside the cursor and closes when the cursor leaves",
                           shown and plot.tip.isHidden()))

        red = next(f for f in dialog._result.findings if f.severity == ERROR and f.check_id == "span")
        kp = 0.5 * (red.kp_start + red.kp_end)
        segment = plot._segment_at(kp)
        worst = plot.findings_at(kp)
        out.append(_report("status bar: the stretch and the findings under the cursor, worst first",
                           segment is not None and segment[2] == 3 and worst and worst[0].severity == ERROR,
                           f"{segment}"))
        plot.depth_plot.setXRange(first, first + 0.05, padding=0)
        plot.statusClicked.emit(segment[0], segment[1], kp)
        rows = results.findings_table.selectionModel().selectedRows()
        selected = results._finding_at(rows[0].row()) if rows else None
        lo, hi = plot.visible_kp()
        out.append(_report("status bar click: the worst finding there is selected and shown",
                           results.currentIndex() == 0 and selected is worst[0]
                           and lo <= min(red.kp_start, red.kp_end) and hi >= max(red.kp_start, red.kp_end)))

        plot.depth_plot.setXRange(1.0, 1.2, padding=0)
        setup.live.setChecked(False)
        setup.value.setValue(3.5)
        dialog.run()
        _wait(lambda: dialog._task is None, 60.0)
        lo, hi = plot.visible_kp()
        out.append(_report("Run keeps the zoomed view when the KPs are the same", abs(lo - 1.0) < 1e-9
                           and abs(hi - 1.2) < 1e-9, f"{lo:.3f} to {hi:.3f}"))

        folded = not (setup.bodies_section.button.isChecked() or setup.sampling_section.button.isChecked()
                      or setup.control_sections_section.button.isChecked())
        bodies = setup.bodies_section.status.text()
        setup.apply_tension(3.0, [(0.0, 1.0, 2.0), (1.0, 3.0, 3.0)])
        out.append(_report("advanced settings start folded with a count; tensions by section open their fold",
                           folded and bodies == "1 body(ies)" and setup.control_sections_section.button.isChecked()
                           and setup.control_sections_section.status.text() == "2 section(s)", bodies))

        tabs = [re.sub(r" \(\d+\)$", "", results.tabText(k)) for k in range(results.count())]   # no counts
        results.show_sweep()
        out.append(_report("results tabs: findings first, then the tools; the sweep sits in Find tension",
                           tabs == ["Findings", "Spans", "Bodies", "Summary", "Find tension", "Detail (EI)"]
                           and results.currentWidget() is results.find_page
                           and results.sweep_section.button.isChecked(), str(tabs)))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_live_update_keeps_the_seabed_still() -> List[bool]:
    """Zoomed on a feature, a change of tension moves the cable only: the profile's axes stay put."""
    out = []
    dialog = _make_dialog()
    try:
        dialog.resize(1500, 950)
        dialog.show()
        dialog.load_example(_example("Verification"))
        _wait(lambda: dialog._task is None, 120.0)
        plot, setup = dialog.plot, dialog.setup

        def painted_range():
            plot.widgets["depth"].grab()          # pyqtgraph re-fits auto-ranged axes as it paints
            QCoreApplication.processEvents()
            return [list(r) for r in plot.depth_plot.vb.viewRange()]

        for true_scale in (False, True):
            plot.true_scale_check.setChecked(true_scale)
            plot.zoom_to(0.48, 0.52)             # as a click on the depression's finding does
            before = painted_range()
            for value in (0.6, 3.0):
                setup.value.setValue(value)
                _wait(lambda: not dialog._live_timer.isActive() and dialog._task is None, 30.0)
            after = painted_range()
            still = all(abs(a - b) < 1e-9 for ra, rb in zip(before, after) for a, b in zip(ra, rb))
            out.append(_report("a live tension change keeps the profile's axes still ("
                               + ("true scale" if true_scale else "free scale") + ")", still,
                               f"{before} -> {after}"))
        plot.true_scale_check.setChecked(False)
    finally:
        dialog.hide()
        dialog.shutdown()
        dialog.deleteLater()
    return out


def coarse_settings(spacing_m: float = 25.0):
    """Rough synthetic seabed known only every ``spacing_m``, light cable at 10 kN."""
    settings = _synthetic_settings()
    settings["route"]["synthetic"].update({"length_m": 3000.0, "spacing_m": spacing_m, "roughness_m": 0.5,
                                           "roughness_length_m": 30.0, "seed": 4})
    settings["seabed"] = {"filter_m": 0.0, "filter_auto": True, "features": []}
    settings["cable"]["bodies"] = []
    settings["control"]["value"] = 10.0
    return settings


def test_data_resolution_in_the_window() -> List[bool]:
    """Coarse data: indicative spans and a flagged stretch, said in the setup; Suit the data restores auto."""
    out = []
    dialog = _make_dialog()
    try:
        setup = dialog.setup
        setup.apply_settings(coarse_settings(25.0))
        dialog.run()
        _wait(lambda: dialog._task is None, 120.0)
        result = dialog._result
        checks = {f.check_id for f in result.findings} if result else set()
        note = setup.data_note.text()
        ok = (result is not None and result.summary.indicative_spans > 0 and "data_resolution" in checks
              and "span_indicative" in checks and "every 25 m" in note and "(auto)" in note
              and "indicative" in dialog.status.text())
        out.append(_report("coarse data: spans indicative, the stretch flagged, the data and filter stated", ok,
                           f"{note} | {dialog.status.text()[:90]}"))
        spans_text = [dialog.results.spans_table.item(r, 12).text() for r in range(dialog.results.spans_table.rowCount())]
        out.append(_report("spans table says which spans the data resolves",
                           any(t.startswith("no: needs 75 m") for t in spans_text), str(spans_text[:3])))

        setup.interval.setValue(5)
        setup.filter_auto.setChecked(False)
        setup.filter_m.setValue(4.0)
        setup.smoothing.setText("20")
        setup.resolve.setValue(2.0)
        setup.suit_the_data()
        restored = (setup.interval.value() == 0 and setup.filter_auto.isChecked() and not setup.filter_m.isEnabled()
                    and setup.smoothing.text() == "" and setup.resolve.value() == 3.0
                    and setup.filter_setting() is None and setup.criteria().resolve_intervals == 3.0)
        saved = setup.settings()["seabed"]
        setup.apply_settings({"seabed": {"filter_m": 2.0}})       # a scenario from before the auto filter
        old_kept = not setup.filter_auto.isChecked() and setup.filter_setting() == 2.0
        out.append(_report("Suit the data restores the auto settings; older scenarios keep their filter",
                           restored and saved.get("filter_auto") is True and old_kept))
    finally:
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_setup_rows_leave_no_gaps() -> List[bool]:
    """Hidden setup rows leave no gap (Qt 5 kept their spacing), and come back in their place."""
    from ..span_analysis import sources as src

    out = []
    dialog = _make_dialog()
    try:
        dialog.resize(1500, 950)
        dialog.show()
        setup = dialog.setup
        setup.apply_settings(_synthetic_settings())
        QCoreApplication.processEvents()
        spacing = setup.route_source.parentWidget().layout().verticalSpacing()
        gap = setup.syn_box.geometry().top() - setup.route_source.geometry().bottom()
        synthetic_ok = gap <= max(spacing, 6) + 8
        setup.route_source.setCurrentIndex(setup.route_source.findData(src.ROUTE_LINE))
        QCoreApplication.processEvents()
        line, start, window = setup.line_row.geometry(), setup.line_start_kp.geometry(), setup.window_row.geometry()
        order_ok = (setup.line_row.isVisible() and not setup.syn_box.isVisible()
                    and setup.route_source.geometry().bottom() < line.top() < start.top() < window.top()
                    and start.top() - line.bottom() <= max(spacing, 6) + 8)
        setup.route_source.setCurrentIndex(setup.route_source.findData(src.ROUTE_SYNTHETIC))
        QCoreApplication.processEvents()
        back_ok = setup.syn_box.isVisible() and not setup.line_row.isVisible()
        out.append(_report("hidden setup rows leave no gap and come back in order", synthetic_ok and order_ok
                           and back_ok, f"gap under Route {gap} px (spacing {spacing}); line row {line.top()}, start "
                           f"KP {start.top()}, window {window.top()}"))
    finally:
        dialog.hide()
        dialog.shutdown()
        dialog.deleteLater()
    return out


def test_sections_open_full_height() -> List[bool]:
    """A section opened inside a form whose rows were hidden and shown gets its full height.

    Under Qt 5 the rebuilt form kept a section's closed height, so In-line
    bodies, Protection and the Seabed subsections opened as a thin sliver.
    """
    from ..span_analysis.setup_panel import CABLE_FIT, CABLE_LIBRARY, Section

    out = []
    dialog = _make_dialog()
    try:
        dialog.resize(1500, 950)
        dialog.show()
        setup = dialog.setup
        setup.apply_settings(_synthetic_settings())
        for cable in (CABLE_FIT, CABLE_LIBRARY):   # hides and shows rows of the cable form
            setup.cable_source.setCurrentIndex(setup.cable_source.findData(cable))
            QCoreApplication.processEvents()
        squashed = []
        for section in setup.findChildren(Section):
            if not section.button.isChecked():
                section.button.click()
            QCoreApplication.processEvents()
        for _ in range(5):
            QCoreApplication.processEvents()
        for section in setup.findChildren(Section):
            need = section.minimumSizeHint().height()
            if section.isVisible() and section.height() < need - 2:
                squashed.append(f"{section.title.strip()} {section.height()} of {need} px")
        out.append(_report("every section opens at its full height", not squashed, "; ".join(squashed)))
    finally:
        dialog.hide()
        dialog.shutdown()
        dialog.deleteLater()
    return out


class _Drag:
    """A pyqtgraph mouse drag (right button) for ViewBox.mouseDragEvent, in view-box coordinates."""

    def __init__(self, start: QPointF, last: QPointF, pos: QPointF, first: bool):
        self._start, self._last, self._pos, self._first = start, last, pos, first

    def accept(self):
        pass

    def isStart(self):
        return self._first

    def isFinish(self):
        return False

    def button(self):
        return Qt.MouseButton.RightButton

    def pos(self):
        return self._pos

    def lastPos(self):
        return self._last

    def screenPos(self):
        return self._pos

    def lastScreenPos(self):
        return self._last

    def buttonDownPos(self, *_args):
        return self._start


def test_true_scale_stays_true() -> List[bool]:
    """True scale holds through pan, zoom, Fit and redraws; a right-button stretch ends it; restored ticked, it locks."""
    from ..span_analysis import plot_panel

    out = []
    dialog = _make_dialog()
    try:
        dialog.resize(1500, 950)
        dialog.show()
        dialog.load_example(_example("Sand waves"))
        _wait(lambda: dialog._task is None, 120.0)
        plot, setup = dialog.plot, dialog.setup
        vb = plot.depth_plot.vb

        def ratio() -> float:
            for _ in range(3):
                QCoreApplication.processEvents()
            sx, sy = vb.viewPixelSize()      # km and m per pixel
            return sx * plot_panel.TRUE_SCALE_RATIO / sy

        plot.true_scale_check.setChecked(True)
        ratios = {"ticked": ratio()}
        vb.translateBy(x=0.2)
        ratios["pan"] = ratio()
        vb.scaleBy(x=0.5, y=1.0)              # the wheel on the KP axis
        ratios["zoom KP"] = ratio()
        vb.scaleBy(x=1.0, y=2.0)              # the wheel on the depth axis
        ratios["zoom depth"] = ratio()
        plot.fit()
        ratios["Fit"] = ratio()
        plot.zoom_to(1.0, 1.1)
        ratios["zoom to a span"] = ratio()
        plot.reverse_check.setChecked(True)
        ratios["reverse KP"] = ratio()
        plot.reverse_check.setChecked(False)
        plot.row_checks["tension"].setChecked(False)
        ratios["a row hidden"] = ratio()
        plot.row_checks["tension"].setChecked(True)
        setup.value.setValue(setup.value.value() + 0.5)   # a live re-run keeps the view
        _wait(lambda: dialog._task is None, 60.0)
        ratios["re-run"] = ratio()
        off = {k: round(v, 3) for k, v in ratios.items() if abs(v - 1.0) > 0.02}
        out.append(_report("true scale stays 1:1 through pan, zoom, Fit, Reverse KP, rows and a re-run",
                           not off and plot.true_scale_check.isChecked(), f"off scale: {off}" if off else ""))

        before = [list(r) for r in vb.viewRange()]
        centre = vb.boundingRect().center()
        vb.mouseDragEvent(_Drag(centre, centre, centre, True))
        kept = all(abs(a - b) < 1e-9 for ra, rb in zip(before, vb.viewRange()) for a, b in zip(ra, rb))
        unticked = not plot.true_scale_check.isChecked() and not vb.state["aspectLocked"]
        vb.mouseDragEvent(_Drag(centre, centre, centre + QPointF(40.0, 0.0), False))   # stretch KP only
        stretched = abs(ratio() - 1.0) > 0.05
        out.append(_report("a right-button drag unticks true scale, keeps the view and stretches freely",
                           unticked and kept and stretched, f"ratio after stretch {ratio():.2f}"))
    finally:
        dialog.hide()
        dialog.shutdown()
        dialog.deleteLater()

    setting = plot_panel._setting
    plot_panel._setting = lambda key, default: True if key == "true_scale" else setting(key, default)
    try:
        fresh = plot_panel.ProfilePlot()
        fresh.persist = False
        out.append(_report("true scale restored ticked from the last session also locks the axes",
                           fresh.true_scale_check.isChecked() and bool(fresh.depth_plot.vb.state["aspectLocked"])))
        fresh.deleteLater()
    finally:
        plot_panel._setting = setting
    return out


def run_all() -> List[bool]:
    results = (test_synthetic_run_live_detail_sweep() + test_raster_route_and_store() + test_ui_tools()
               + test_tables_and_dxf_route() + test_protection_and_find_tension() + test_kp_consistency()
               + test_rpl_cable_mapping() + test_follows_project() + test_plot_and_layout_controls()
               + test_live_update_keeps_the_seabed_still() + test_setup_rows_leave_no_gaps()
               + test_sections_open_full_height() + test_true_scale_stays_true()
               + test_data_resolution_in_the_window())
    print(f"\n{sum(results)}/{len(results)} Span & Slack Analysis QGIS checks passed.")
    return results
