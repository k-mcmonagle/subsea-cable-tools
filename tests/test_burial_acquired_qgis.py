# -*- coding: utf-8 -*-
"""Acquired burial data with QGIS: sample geometry, the data file's place, the KP row stack.

* The sample deliveries agree with QGIS: their UTM eastings and northings
  match QGIS's own transform to the millimetre, and the plugin's KP and DCC
  (RouteFrame) give back the KPs and offsets the positions were made from.
* The data file follows the plugin's rule for projects: a project that
  never used acquired data gets no file and no project entry from asking
  where it would go; the file and the entry appear when the user acts, the
  file sits beside the plan file, and clearing the project forgets it.
* The shared KP row stack: KP labels on the lowest shown row, at least one
  row kept, merged status stretches, the crosshair and readout on hover,
  fit, reverse KP, legends. A screenshot is saved for a look.
"""

from __future__ import annotations

import math
import os
import tempfile

import numpy as np

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject)
from qgis.PyQt.QtCore import QCoreApplication

from ..burial.acquired import location
from ..burial.acquired import samples as S
from ..burial.acquired.store import AcquiredStore
from ..kp_geo_utils import RouteFrame, measure_total_length_m
from ..kp_range_utils import make_distance_area
from ..plotkit.row_stack import KpRowStack
from ..plotkit.widgets import legend_html

REQUIRES_QGIS = True


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


def test_samples_match_qgis() -> bool:
    route = S.SyntheticRoute()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    transform = QgsCoordinateTransform(wgs84, QgsCoordinateReferenceSystem(f"EPSG:{S.UTM_EPSG}"),
                                       QgsProject.instance().transformContext())
    rng = np.random.default_rng(5)
    lon = rng.uniform(2.6, 3.4, 40)
    lat = rng.uniform(54.8, 55.3, 40)
    easting, northing = S.utm(lon, lat)
    utm_error = max(math.hypot(transform.transform(QgsPointXY(x, y)).x() - e,
                               transform.transform(QgsPointXY(x, y)).y() - n)
                    for x, y, e, n in zip(lon, lat, easting, northing))
    geom = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in S.ROUTE_LONLAT])
    distance = make_distance_area(wgs84)
    frame = RouteFrame([geom], [measure_total_length_m(geom, distance)], distance)
    kps = np.array([0.5, 5.0, 12.3, 20.0, 30.0])
    on_lon, on_lat = route.position(kps)
    kp_error = max(abs(frame.kp_at_point(QgsPointXY(x, y)).kp_km - k) * 1000.0 for x, y, k in zip(on_lon, on_lat, kps))
    off_lon, off_lat = route.position(kps, 10.0)
    hits = [frame.kp_at_point(QgsPointXY(x, y)) for x, y in zip(off_lon, off_lat)]
    print(f"  utm {utm_error:.6f} m; kp {kp_error:.4f} m; dcc {[round(float(h.dcc_m), 3) for h in hits]}; "
          f"kp off {[round(float(h.kp_km - k) * 1000, 3) for h, k in zip(hits, kps)]} m")
    return _check("samples agree with QGIS's transform and the plugin's KP", {
        "utm to a millimetre": utm_error < 0.001,
        "route length": abs(frame.total_length_km - route.length_km) < 1e-6,
        "kp on the route": kp_error < 0.01,
        "dcc 10 m": all(abs(h.dcc_m - 10.0) < 0.05 for h in hits),
        "kp beside the route": all(abs(h.kp_km - k) * 1000.0 < 1.0 for h, k in zip(hits, kps)),
    })


def test_data_file_location() -> bool:
    project = QgsProject.instance()
    project.clear()
    default = location.default_data_path(project)
    existed = os.path.exists(default)
    state_new = location.resolve(project)
    entry_new = location.project_data_path(project)
    folder = tempfile.mkdtemp(prefix="ba_location_")
    project.setFileName(os.path.join(folder, "Cable S1.qgz"))
    beside = location.default_data_path(project)
    store = location.open_or_create(project)
    store.close()
    state_used = location.resolve(project)
    project.clear()
    state_cleared = location.resolve(project)
    return _check("data file: nothing until the user acts; beside the plan file; forgotten on clear", {
        "no file made by asking": os.path.exists(default) == existed,
        "no entry": state_new == (None, location.STATE_NONE) and entry_new is None,
        "beside the plan file": beside == os.path.join(folder, "Cable S1_burial_data.gpkg"),
        "plans name": location.data_path_for_plans(r"C:\x\Job_burial_plans.gpkg") == os.path.join(
            r"C:\x", "Job_burial_data.gpkg") and location.data_path_for_plans("/x/plans.gpkg").endswith(
            "plans_burial_data.gpkg"),
        "created on use": os.path.isfile(beside) and state_used == (beside, location.STATE_OK),
        "forgotten on clear": state_cleared == (None, location.STATE_NONE),
        "file kept": AcquiredStore(beside).open().list_datasets() == [],
    })


def test_row_stack() -> bool:
    levels = [(0, (200, 200, 200), "No data"), (1, (76, 175, 80), "Meets target"), (2, (211, 47, 47), "Below target")]
    stack = KpRowStack("Tests/AcquiredRowStack", levels=levels, persist=False)
    stack.legend_action.setChecked(True)
    stack.reverse_action.setChecked(False)
    dol = stack.add_row("dol", "DOL", y_label="DOL (m)", invert_y=True, height=300,
                        legend=legend_html([("solid", (25, 70, 160), "DOL"), ("dash", (0, 128, 0), "Target")]))
    stack.add_row("wd", "Water depth", y_label="WD (m)", invert_y=True)
    stack.add_row("tension", "Tension", y_label="T (kN)", legend=legend_html([("solid", (90, 90, 90), "Tow")]))
    for action in stack.row_actions.values():
        action.setChecked(True)
    kp = np.linspace(0.0, 10.0, 5001)
    line = stack.curve("dol", (25, 70, 160))
    line.setData(kp, S.dol_profile(kp))
    stack.curve("dol", (0, 128, 0), style=None, fast=False).setData([0.0, 10.0], [1.0, 1.0])
    stack.curve("wd", (121, 85, 72)).setData(kp, S.water_depth(kp))
    stack.curve("tension", (90, 90, 90)).setData(kp, 250 + 70 * S.dol_profile(kp))
    edges = np.linspace(0.0, 10.0, 11)
    levels_per_km = np.array([1, 1, 1, 2, 1, 1, 1, 2, 0, 1])
    stack.set_status(edges[:-1], edges[1:], levels_per_km)
    stack.readout_provider = lambda k: f"KP {k:.3f}"
    stack.tip_provider = lambda k: f"<b>KP {k:.3f}</b>"
    hovered = []
    stack.kpHovered.connect(hovered.append)
    stack.set_extent(0.0, 10.0)
    stack.resize(900, 640)
    stack.show()
    QCoreApplication.processEvents()
    stack.fit()
    QCoreApplication.processEvents()

    def bottom(key):
        return stack.plots[key].getAxis("bottom").isVisible()

    lowest_first = bottom("tension") and not bottom("dol") and not bottom("wd")
    stack.row_actions["tension"].setChecked(False)
    lowest_after = bottom("wd") and not bottom("tension")
    for action in stack.row_actions.values():
        action.setChecked(False)
    kept = stack.visible_rows()
    for action in stack.row_actions.values():
        action.setChecked(True)
    stretch = stack.status_at(1.5)
    hovered.clear()
    stack.hover(5.0)
    lines_shown = all(line.isVisible() for line in stack._vlines)
    readout = stack.readout.text()
    stack.end_hover()
    lines_hidden = not any(line.isVisible() for line in stack._vlines)
    lo, hi = stack.visible_kp()
    stack.reverse_action.setChecked(True)
    reversed_all = all(p.vb.xInverted() for p in list(stack.plots.values()) + [stack.status_plot])
    stack.reverse_action.setChecked(False)
    QCoreApplication.processEvents()
    shot = os.path.join(tempfile.mkdtemp(prefix="row_stack_"), "row_stack.png")
    stack.grab().save(shot)
    print(f"  screenshot: {shot}")
    stack.legend_action.setChecked(False)
    legends_hidden = not any(c.isVisible() for c in stack.captions.values())
    stack.hide()
    stack.deleteLater()
    return _check("KP row stack: rows, status, hover, fit, reverse, legends", {
        "kp labels on the lowest row": lowest_first and lowest_after,
        "one row kept": kept == ["dol"],
        "status merged": stretch == (0.0, 3.0, 1),
        "status below": stack.status_at(3.5)[2] == 2 if stack.status_at(3.5) else False,
        "hover": lines_shown and readout == "KP 5.000" and hovered[0] == 5.0,
        "end hover": lines_hidden and math.isnan(hovered[-1]),
        "fit": lo < 0.0 < 10.0 < hi and hi - lo < 10.5,
        "reverse": reversed_all,
        "legends": legends_hidden,
        "screenshot": os.path.getsize(shot) > 5000,
        "dol plot": dol is stack.plots["dol"],
    })


def run_all() -> list:
    return [
        test_samples_match_qgis(),
        test_data_file_location(),
        test_row_stack(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
