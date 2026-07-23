# -*- coding: utf-8 -*-
"""QGIS 3/4 integration checks for Cable Lay Simulator map outputs."""

from __future__ import annotations

import numpy as np

from qgis.core import QgsProject
from qgis.gui import QgsMapCanvas

from ..catenary.v3.ui.map_tools import SimulatorMapOverlay
from ..catenary.v3.ui.qgis_adapters import (
    push_chains_to_map,
    push_markers_to_map,
)
from ..catenary.v3.ui.scene import (
    CablePath,
    Marker,
    SceneData,
    VesselGlyph,
)


def test_results_to_map_fields_and_geometry():
    project = QgsProject.instance()
    layers = []
    try:
        line_layer = push_chains_to_map(
            "V3 compatibility cable",
            [("main", np.array([[0.0, 0.0, -10.0],
                                 [25.0, 5.0, -12.0]]))],
            (0.0, 0.0),
            "EPSG:3857",
        )
        layers.append(line_layer)
        assert line_layer.fields().indexOf("name") >= 0
        assert line_layer.featureCount() == 1
        assert next(line_layer.getFeatures())["name"] == "main"

        marker_layer = push_markers_to_map(
            "V3 compatibility markers",
            [("TDP", (25.0, 5.0, -12.0))],
            (0.0, 0.0),
            "EPSG:3857",
        )
        layers.append(marker_layer)
        assert marker_layer.fields().indexOf("label") >= 0
        assert marker_layer.featureCount() == 1
        assert next(marker_layer.getFeatures())["label"] == "TDP"
    finally:
        for layer in layers:
            project.removeMapLayer(layer.id())


def test_canvas_overlay_line_polygon_and_marker():
    canvas = QgsMapCanvas()
    overlay = SimulatorMapOverlay(canvas)
    scene = SceneData(
        cables=[CablePath(np.array([[0.0, 0.0, -5.0],
                                    [20.0, 3.0, -10.0]]))],
        markers=[Marker((20.0, 3.0, -10.0), "TDP", "tdp")],
        vessel=VesselGlyph((0.0, 0.0)),
    )

    overlay.update(scene, (0.0, 0.0), "EPSG:3857")
    assert len(overlay._cable_bands) == 1
    assert overlay._vessel_band is not None
    assert len(overlay._markers) == 1
    overlay.clear()
    assert overlay._cable_bands == []
    assert overlay._vessel_band is None
    assert overlay._markers == []


def run_all():
    failures = []
    for test in (
            test_results_to_map_fields_and_geometry,
            test_canvas_overlay_line_polygon_and_marker):
        try:
            test()
            print("[PASS] %s" % test.__name__)
        except Exception as exc:
            print("[FAIL] %s - %r" % (test.__name__, exc))
            failures.append(test.__name__)
    return failures


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
