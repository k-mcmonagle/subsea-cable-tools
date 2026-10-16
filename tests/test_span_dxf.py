# -*- coding: utf-8 -*-
"""Span & Slack Analysis DXF: profile and shape import, scene export (pure Python)."""

from __future__ import annotations

import math
import os
import tempfile
from typing import List

import numpy as np

from ..cable_seabed import planning as pl
from ..cable_seabed.features import CUSTOM, SeabedFeature, apply_features, format_shape, parse_shape
from ..span_analysis import dxf_io

REQUIRES_QGIS = False


def _report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    return bool(ok)


def _folder() -> str:
    return tempfile.mkdtemp(prefix="span_dxf_")


def test_profile_round_trip() -> bool:
    """A profile drawn at 10x vertical exaggeration (elevation, chainage) reads back as depth by KP."""
    x = np.arange(0.0, 500.0001, 2.0)
    depth = 100.0 + 2.0 * np.sin(x / 40.0)
    writer = dxf_io.DxfWriter()
    seabed = writer.layer("SEABED", 3)
    other = writer.layer("TEXT", 7)
    # Drawn in two pieces on purpose: they are joined.
    half = len(x) // 2
    writer.polyline(zip(x[:half + 1], -depth[:half + 1] * 10.0), seabed)
    writer.polyline(zip(x[half:], -depth[half:] * 10.0), seabed)
    writer.line(0.0, 0.0, 10.0, 10.0, other)
    path = os.path.join(_folder(), "profile.dxf")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(writer.document())
    layers = dxf_io.read_polylines(path)
    options = dxf_io.ProfileDxfOptions("SEABED", dxf_io.X_CHAINAGE_M, 12.0, dxf_io.Y_ELEVATION, 10.0)
    profile = dxf_io.profile_from_dxf(path, options)
    ok = set(layers) == {"SEABED", "TEXT"} and len(profile["kp_km"]) == len(x) \
        and abs(profile["kp_km"][0] - 12.0) < 1e-9 and abs(profile["kp_km"][-1] - 12.5) < 1e-9 \
        and np.allclose(profile["depth_m"], depth, atol=1e-3)
    return _report("DXF profile: chainage + elevation at 10x read back as depth by KP", ok)


def test_lwpolyline_and_shape() -> bool:
    """An LWPOLYLINE berm drawn on a 2% slope in millimetres: heights above its end-to-end base."""
    xs = [0.0, 4500.0, 8000.0, 12000.0, 15500.0, 20000.0]
    heights = [0.0, 1500.0, 1800.0, 1800.0, 1500.0, 0.0]
    ys = [h + 0.02 * x for x, h in zip(xs, heights)]
    text = "0\nSECTION\n2\nENTITIES\n0\nLWPOLYLINE\n8\nBERM\n90\n6\n70\n0\n"
    for x, y in zip(xs, ys):
        text += f"10\n{x}\n20\n{y}\n"
    text += "0\nENDSEC\n0\nEOF\n"
    path = os.path.join(_folder(), "berm.dxf")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    points = dxf_io.shape_from_dxf(path, "BERM", scale=0.001)
    expect = [(-10.0, 0.0), (-5.5, 1.5), (-2.0, 1.8), (2.0, 1.8), (5.5, 1.5), (10.0, 0.0)]
    ok = len(points) == 6 and all(abs(a - c) < 1e-6 and abs(b - d) < 1e-6 for (a, b), (c, d) in zip(points, expect))
    back = parse_shape(format_shape(points))
    return _report("DXF shape: LWPOLYLINE in mm on a slope gives the berm's offsets and heights", ok
                   and len(back) == 6)


def test_custom_feature() -> bool:
    """A custom rock berm: heights at its points, stations at each point, a span either side at 10 kN."""
    shape = parse_shape("-10 0; -5.5 1.5; -2 1.8; 2 1.8; 5.5 1.5; 10 0")
    berm = SeabedFeature(CUSTOM, 0.2, 0.0, 0.0, 0.0, "berm", shape)
    x = np.arange(0.0, 400.0001, 1.0)
    new_x, _kp, height, _ = apply_features(x, x / 1000.0, [berm], np.zeros(len(x)))
    at = {float(o): float(np.interp(200.0 + o, new_x, height)) for o, _h in shape}
    ok_shape = all(abs(at[o] - h) < 1e-9 for o, h in shape) and all(np.isin(200.0 + np.array([o for o, _ in shape]),
                                                                            new_x))
    cable = pl.CableType("DA", w_npm=60.0, ei_knm2=20.0, mbr_m=2.0, diameter_m=0.05, npts_kn=60.0)
    prof = pl.Profile(x, x / 1000.0, np.full(len(x), 50.0))
    result = pl.run(pl.PlanningInputs(prof, cable, features=[berm], control=pl.LayControl(pl.TENSION, 10.0),
                                      filter_m=0.0, criteria=pl.Criteria(allowable=False)))
    expect_side = math.sqrt(2.0 * 1.5 * 10000.0 / 60.0)   # from the berm's 1.5 m shoulder
    spans = result.spans
    ok_spans = len(spans) >= 2 and max(s.length_m for s in spans) > 0.8 * expect_side
    bad = False
    try:
        parse_shape("-1 0; 2")
    except ValueError:
        bad = True
    return _report("custom feature: shape, stations and spans over a rock berm", ok_shape and ok_spans and bad,
                   f"{len(spans)} spans, longest {max((s.length_m for s in spans), default=0):.1f} m")


def test_scene_export() -> bool:
    x = np.arange(0.0, 600.0001, 0.5)
    prof = pl.Profile(x, 3.0 + x / 1000.0, np.full(len(x), 100.0))
    cable = pl.CableType("LW", w_npm=10.0, ei_knm2=1.0, mbr_m=1.5, diameter_m=0.02, npts_kn=20.0)
    features = [SeabedFeature("boulder", 3.3, 1.0, 2.0)]
    bodies = [pl.Body("R1", 3.45, 3.0, 3.0, 0.3, "Repeater")]
    result = pl.run(pl.PlanningInputs(prof, cable, features=features, bodies=bodies,
                                      control=pl.LayControl(pl.TENSION, 5.0), filter_m=0.0))
    path = os.path.join(_folder(), "scene.dxf")
    layers = dxf_io.export_scene(result, path, dxf_io.SceneOptions(vertical_exaggeration=10.0))
    read = dxf_io.read_polylines(path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    needed = {"SA_SEABED", "SA_CABLE", "SA_SUPPORTS", "SA_BODIES", "SA_FEATURES", "SA_SEA_LEVEL", "SA_FRAME",
              "SA_TENSION_RESIDUAL", "SA_SLACK_LAID", "SA_SLOPE_FRICTION_ANGLE"}
    spans = {"SA_SPAN_RED", "SA_SPAN_AMBER"} & set(read)
    seabed = dxf_io.merged_points(read["SA_SEABED"])
    ok = needed <= set(layers) and needed - {"SA_SUPPORTS"} <= set(read) and spans \
        and abs(seabed[0, 0] - 3000.0) < 1e-6 and abs(seabed[0, 1] + 1000.0) < 1e-3 \
        and "R1 (Repeater)" in text and text.rstrip().endswith("EOF")
    return _report("scene DXF: layers by item, X = KP x 1000, Y = elevation x VE", ok, ", ".join(sorted(read))[:160])


def run_all() -> List[bool]:
    results = [test_profile_round_trip(), test_lwpolyline_and_shape(), test_custom_feature(), test_scene_export()]
    print(f"\n{sum(results)}/{len(results)} Span & Slack Analysis DXF checks passed.")
    return results


if __name__ == "__main__":
    run_all()
