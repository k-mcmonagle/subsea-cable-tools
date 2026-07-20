# -*- coding: utf-8 -*-
"""Regression tests guarding the V3 solver performance optimisations.

Pure Python + NumPy; no QGIS imports. Each test compares a rewritten fast
path against its retained reference implementation, or pins behaviour the
optimisations must not change. End-to-end accuracy is gated separately by
``bench_v3_solver.py --compare``.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_engine():
    pkg = types.ModuleType("sct_v3_reg")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_reg"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "timeline", "scenarios"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_reg.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py"
        )
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_reg.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
bathy_mod = M["bathymetry"]


def _assert(cond, msg):
    assert cond, msg


def _smooth_grid(n=120, extent=1200.0, depth0=60.0):
    """Gaussian bump + tilt: a smooth analytic-ish surface."""
    xs = np.linspace(-extent / 2, extent / 2, n)
    X, Y = np.meshgrid(xs, xs)
    depths = depth0 + 8.0 * np.exp(-((X - 100) ** 2 + (Y + 50) ** 2) / (2 * 180.0 ** 2)) + 0.01 * X
    d = extent / (n - 1)
    return bathy_mod.GridBathymetry(-extent / 2, -extent / 2, d, d, depths)


def _rough_grid(n=120, extent=1200.0, depth0=60.0, amp=6.0, seed=7):
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal((n, n))
    for _ in range(4):
        noise = 0.25 * (np.roll(noise, 1, 0) + np.roll(noise, -1, 0)
                        + np.roll(noise, 1, 1) + np.roll(noise, -1, 1))
    noise = noise / max(1e-9, float(np.std(noise)))
    d = extent / (n - 1)
    return bathy_mod.GridBathymetry(-extent / 2, -extent / 2, d, d, depth0 + amp * noise)


def _normal_angle_deg(gx1, gy1, gx2, gy2):
    """Angle between bed normals (gx, gy, 1)/|.| for two gradient fields."""
    n1 = np.stack([gx1, gy1, np.ones_like(gx1)], axis=-1)
    n2 = np.stack([gx2, gy2, np.ones_like(gx2)], axis=-1)
    n1 = n1 / np.linalg.norm(n1, axis=-1, keepdims=True)
    n2 = n2 / np.linalg.norm(n2, axis=-1, keepdims=True)
    dot = np.clip(np.sum(n1 * n2, axis=-1), -1.0, 1.0)
    return np.degrees(np.arccos(dot))


def test_grid_gradient_matches_central_reference():
    """The precomputed half-step gradient tables must reproduce the
    half-cell central-difference reference EXACTLY (both are the same
    piecewise-bilinear field; the tables just sample its breakpoints), on
    smooth AND rough grids, everywhere inside the grid extent including the
    clamped edge bands."""
    rng = np.random.default_rng(123)
    for label, grid in (("smooth", _smooth_grid()), ("rough", _rough_grid())):
        ny, nx = grid.depths.shape
        xs = rng.uniform(grid.x0, grid.x0 + (nx - 1) * grid.dx, 10000)
        ys = rng.uniform(grid.y0, grid.y0 + (ny - 1) * grid.dy, 10000)
        gx1, gy1 = grid.grad_at(xs, ys)
        gx2, gy2 = grid._grad_central_reference(xs, ys)
        dmax = max(
            float(np.max(np.abs(np.asarray(gx1) - np.asarray(gx2)))),
            float(np.max(np.abs(np.asarray(gy1) - np.asarray(gy2)))),
        )
        _assert(dmax < 1e-7, f"{label}: gradient table deviates from reference by {dmax:.2e}")
        # Sanity: the implied bed normals are then trivially identical.
        ang = _normal_angle_deg(np.asarray(gx1), np.asarray(gy1),
                                np.asarray(gx2), np.asarray(gy2))
        _assert(float(np.max(ang)) < 1e-5, "normals must be identical")


def test_grid_gradient_zero_outside_extent():
    """Depth is clamped flat outside the grid; the gradient must be zero
    there so the contact normal matches the clamped surface."""
    grid = _smooth_grid()
    far = grid.x0 - 500.0
    gx, gy = grid.grad_at(np.array([far, far]), np.array([0.0, far]))
    _assert(float(np.max(np.abs(gx))) == 0.0 and float(np.max(np.abs(gy))) == 0.0,
            "gradient outside the grid extent must be exactly zero")
    # Scalar path too.
    gxs, gys = grid.grad_at(far, 0.0)
    _assert(gxs == 0.0 and gys == 0.0, "scalar outside-gradient must be zero")


def test_grid_gradient_scalar_and_array_consistent():
    grid = _rough_grid()
    x, y = 37.5, -212.0
    gxs, gys = grid.grad_at(x, y)
    gxa, gya = grid.grad_at(np.array([x]), np.array([y]))
    _assert(abs(gxs - float(gxa[0])) < 1e-12 and abs(gys - float(gya[0])) < 1e-12,
            "scalar and array grad_at must agree")


def test_grid_gradient_matches_slope_on_planar_grid():
    """On an exactly planar gridded surface both the interpolated and the
    reference gradients must reproduce the plane's slope exactly (interior)."""
    n, extent = 60, 600.0
    xs = np.linspace(0.0, extent, n)
    X, Y = np.meshgrid(xs, xs)
    gx_true, gy_true = 0.05, -0.02
    depths = 50.0 + gx_true * X + gy_true * Y
    d = extent / (n - 1)
    grid = bathy_mod.GridBathymetry(0.0, 0.0, d, d, depths)
    rng = np.random.default_rng(5)
    px = rng.uniform(2 * d, extent - 2 * d, 500)
    py = rng.uniform(2 * d, extent - 2 * d, 500)
    gx, gy = grid.grad_at(px, py)
    _assert(float(np.max(np.abs(np.asarray(gx) - gx_true))) < 1e-9,
            "planar gx must be exact")
    _assert(float(np.max(np.abs(np.asarray(gy) - gy_true))) < 1e-9,
            "planar gy must be exact")


# ---------------------------------------------------------------------------

def _result(name: str, ok: bool, detail: str = ""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def run_all():
    failures = []
    tests = [
        test_grid_gradient_matches_central_reference,
        test_grid_gradient_zero_outside_extent,
        test_grid_gradient_scalar_and_array_consistent,
        test_grid_gradient_matches_slope_on_planar_grid,
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
