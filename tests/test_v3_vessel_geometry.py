# -*- coding: utf-8 -*-
"""Vessel/scene geometry checks for the Cable Lay Simulator (3D).

Covers the parametric hull placement (CRP + chute offsets, heading), the
compass <-> math frame conversion, the drawn chute arc, and the map
bearing helper. Pure Python + NumPy; no QGIS imports.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scene = _load("sct_v3_scene_geom", "catenary/v3/ui/scene.py")
map_tools = _load("sct_v3_map_tools", "catenary/v3/ui/map_tools.py")


def test_compass_math_roundtrip():
    assert scene.compass_to_math_deg(0.0) == 90.0      # north -> +y
    assert scene.compass_to_math_deg(90.0) == 0.0      # east -> +x
    for b in (0.0, 37.5, 90.0, 180.0, 271.0):
        assert abs(scene.math_to_compass_deg(scene.compass_to_math_deg(b)) - b) < 1e-9


def test_footprint_default_centred_on_chute():
    v = scene.VesselGlyph(xy=(10.0, -5.0), heading_deg=0.0, length_m=60.0, beam_m=12.0)
    foot = scene.vessel_footprint(v)
    assert foot.shape == (5, 2)
    assert abs(foot[:, 0].max() - (10.0 + 30.0)) < 1e-9   # bow
    assert abs(foot[:, 0].min() - (10.0 - 30.0)) < 1e-9   # stern
    assert abs(foot[:, 1].max() - (-5.0 + 6.0)) < 1e-9


def test_footprint_chute_offset_places_stern_at_anchor():
    # Chute 30 m aft of a midship CRP on a 60 m ship: stern lands on xy.
    v = scene.VesselGlyph(xy=(0.0, 0.0), heading_deg=0.0, length_m=60.0,
                          beam_m=12.0, chute_fwd_m=-30.0)
    foot = scene.vessel_footprint(v)
    assert abs(foot[:, 0].min() - 0.0) < 1e-9             # stern at the chute
    assert abs(foot[:, 0].max() - 60.0) < 1e-9            # bow forward of it
    crp = scene.vessel_crp_xy(v)
    assert abs(crp[0] - 30.0) < 1e-9 and abs(crp[1]) < 1e-9


def test_footprint_heading_rotation():
    # Math heading 90 deg = +y; the bow must point along +y.
    v = scene.VesselGlyph(xy=(0.0, 0.0), heading_deg=90.0, length_m=60.0, beam_m=12.0)
    foot = scene.vessel_footprint(v)
    assert abs(foot[:, 1].max() - 30.0) < 1e-9
    assert abs(foot[:, 0].max() - 6.0) < 1e-6


def test_starboard_offset_sign():
    # Chute 5 m to starboard of the CRP, heading +x: starboard is -y in the
    # world (math frame), so the hull centreline must sit at +5 y.
    v = scene.VesselGlyph(xy=(0.0, 0.0), heading_deg=0.0, length_m=60.0,
                          beam_m=12.0, chute_stbd_m=5.0)
    foot = scene.vessel_footprint(v)
    mid_y = 0.5 * (foot[:, 1].max() + foot[:, 1].min())
    assert abs(mid_y - 5.0) < 1e-9


def test_chute_point_and_arc():
    v = scene.VesselGlyph(xy=(3.0, 4.0), heading_deg=0.0, height_m=7.0,
                          chute_radius_m=2.0)
    top = scene.vessel_chute_xyz(v, water_z=0.0)
    assert top == (3.0, 4.0, 7.0)
    arc = scene.chute_arc_points(v, water_z=0.0, n=9)
    assert arc.shape == (9, 3)
    assert np.allclose(arc[0], top)                       # starts at chute top
    # Ends a radius down and a radius aft (heading +x -> aft is -x).
    assert np.allclose(arc[-1], (1.0, 4.0, 5.0))
    centre = np.array([3.0, 4.0, 5.0])
    assert np.allclose(np.linalg.norm(arc - centre, axis=1), 2.0)
    assert scene.chute_arc_points(scene.VesselGlyph(xy=(0, 0)), 0.0) is None


def test_bearing_deg():
    assert abs(map_tools.bearing_deg((0, 0), (0, 10)) - 0.0) < 1e-9    # north
    assert abs(map_tools.bearing_deg((0, 0), (10, 0)) - 90.0) < 1e-9   # east
    assert abs(map_tools.bearing_deg((0, 0), (0, -10)) - 180.0) < 1e-9
    assert abs(map_tools.bearing_deg((0, 0), (-10, 0)) - 270.0) < 1e-9


def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_compass_math_roundtrip,
        test_footprint_default_centred_on_chute,
        test_footprint_chute_offset_places_stern_at_anchor,
        test_footprint_heading_rotation,
        test_starboard_offset_sign,
        test_chute_point_and_arc,
        test_bearing_deg,
    ]
    for test in tests:
        try:
            test()
            _result(test.__name__, True)
        except Exception as exc:  # pragma: no cover
            _result(test.__name__, False, repr(exc))
            failures.append(test.__name__)
    print(f"\n{len(failures)} failure(s)." if failures else "\nAll checks passed.")
    return failures


if __name__ == "__main__":  # pragma: no cover
    failures = run_all()
    sys.exit(1 if failures else 0)
