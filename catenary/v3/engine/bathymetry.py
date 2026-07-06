# -*- coding: utf-8 -*-
"""Seabed surfaces for the 3D lay engine.

Pure Python + NumPy; no Qt/QGIS imports.

Frame convention: ``x, y`` horizontal metres, ``z`` vertical metres with 0 at
the sea surface and negative down. A bathymetry maps horizontal position to
**positive depth** ``D(x, y)``; the bed elevation is ``z_bed = -D``.

All ``depth_at`` / ``grad_at`` implementations accept scalars or NumPy arrays
and are vectorised — the contact loop of the solver evaluates them on every
node each iteration.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import List, Sequence, Tuple

import numpy as np


class Bathymetry:
    """Interface: positive depth and its horizontal gradient."""

    def depth_at(self, x, y):
        raise NotImplementedError

    def grad_at(self, x, y) -> Tuple["np.ndarray", "np.ndarray"]:
        """(dD/dx, dD/dy) — same shape as the inputs."""
        raise NotImplementedError

    def z_at(self, x, y):
        return -self.depth_at(x, y)

    def to_dict(self) -> dict:
        raise NotImplementedError


@dataclass
class FlatBathymetry(Bathymetry):
    depth_m: float

    def depth_at(self, x, y):
        shape = np.broadcast(np.asarray(x), np.asarray(y)).shape
        if shape == ():
            return float(self.depth_m)
        return np.full(shape, float(self.depth_m))

    def grad_at(self, x, y):
        shape = np.broadcast(np.asarray(x), np.asarray(y)).shape
        if shape == ():
            return 0.0, 0.0
        return np.zeros(shape), np.zeros(shape)

    def to_dict(self) -> dict:
        return {"kind": "flat", "depth_m": float(self.depth_m)}


@dataclass
class PlanarSlopeBathymetry(Bathymetry):
    """Depth varying linearly: ``D = depth0 + gx*x + gy*y``.

    ``gx``/``gy`` are d(depth)/d(distance); e.g. a 5-degree down slope toward
    +x is ``gx = tan(5 deg)``, ``gy = 0``.
    """

    depth0_m: float
    gx: float = 0.0
    gy: float = 0.0

    def depth_at(self, x, y):
        return self.depth0_m + self.gx * np.asarray(x, dtype=float) + self.gy * np.asarray(y, dtype=float)

    def grad_at(self, x, y):
        shape = np.broadcast(np.asarray(x), np.asarray(y)).shape
        return np.full(shape, float(self.gx)), np.full(shape, float(self.gy))

    def to_dict(self) -> dict:
        return {"kind": "slope", "depth0_m": float(self.depth0_m), "gx": float(self.gx), "gy": float(self.gy)}


class ProfileBathymetry(Bathymetry):
    """Piecewise-linear depth along an azimuth, uniform transverse.

    ``points`` are (distance_along_m, depth_m) pairs; distance is measured
    along the unit direction ``azimuth_deg`` (0 = +x, counter-clockwise
    positive, i.e. mathematical convention). This reproduces the V2
    "Profile" seabed extruded sideways. Depth is clamped to the end values
    outside the profile extent.
    """

    def __init__(self, points: Sequence[Tuple[float, float]], azimuth_deg: float = 0.0):
        pts = sorted((float(d), float(z)) for d, z in points)
        if len(pts) < 1:
            raise ValueError("ProfileBathymetry needs at least one point.")
        if len(pts) == 1:
            pts.append((pts[0][0] + 1.0, pts[0][1]))
        self._d = np.array([p[0] for p in pts], dtype=float)
        self._depth = np.array([p[1] for p in pts], dtype=float)
        if np.any(np.diff(self._d) <= 0):
            # de-duplicate identical stations
            keep = np.concatenate([[True], np.diff(self._d) > 1e-9])
            self._d = self._d[keep]
            self._depth = self._depth[keep]
            if len(self._d) < 2:
                self._d = np.array([self._d[0], self._d[0] + 1.0])
                self._depth = np.array([self._depth[0], self._depth[0]])
        self.azimuth_deg = float(azimuth_deg)
        a = math.radians(self.azimuth_deg)
        self._ux, self._uy = math.cos(a), math.sin(a)
        # Per-interval slopes for the gradient.
        self._slope = np.diff(self._depth) / np.diff(self._d)

    @property
    def points(self) -> List[Tuple[float, float]]:
        return [(float(d), float(z)) for d, z in zip(self._d, self._depth)]

    def _s_along(self, x, y):
        return np.asarray(x, dtype=float) * self._ux + np.asarray(y, dtype=float) * self._uy

    def depth_at(self, x, y):
        s = self._s_along(x, y)
        return np.interp(s, self._d, self._depth, left=self._depth[0], right=self._depth[-1])

    def grad_at(self, x, y):
        s = np.atleast_1d(self._s_along(x, y)).astype(float)
        idx = np.clip(np.searchsorted(self._d, s, side="right") - 1, 0, len(self._slope) - 1)
        slope = self._slope[idx]
        slope = np.where((s < self._d[0]) | (s > self._d[-1]), 0.0, slope)
        gx = slope * self._ux
        gy = slope * self._uy
        if np.ndim(x) == 0 and np.ndim(y) == 0:
            return float(gx[0]), float(gy[0])
        return gx, gy

    def to_dict(self) -> dict:
        return {"kind": "profile", "points": self.points, "azimuth_deg": self.azimuth_deg}


class GridBathymetry(Bathymetry):
    """Regularly gridded depths with bilinear interpolation.

    ``depths`` is (ny, nx) positive metres; node (i, j) sits at
    ``(x0 + j*dx, y0 + i*dy)``. Outside the grid the edge value is used
    (clamped), with zero gradient beyond the edge.
    """

    def __init__(self, x0: float, y0: float, dx: float, dy: float, depths):
        self.x0, self.y0 = float(x0), float(y0)
        self.dx, self.dy = float(dx), float(dy)
        if self.dx <= 0 or self.dy <= 0:
            raise ValueError("dx and dy must be positive.")
        self.depths = np.asarray(depths, dtype=float)
        if self.depths.ndim != 2 or self.depths.shape[0] < 2 or self.depths.shape[1] < 2:
            raise ValueError("depths must be a 2D array of at least 2x2.")
        if not np.all(np.isfinite(self.depths)):
            # Fill nodata with the finite mean so the solver never sees NaN.
            finite = self.depths[np.isfinite(self.depths)]
            fill = float(finite.mean()) if finite.size else 0.0
            self.depths = np.where(np.isfinite(self.depths), self.depths, fill)

    def _local(self, x, y):
        fx = (np.asarray(x, dtype=float) - self.x0) / self.dx
        fy = (np.asarray(y, dtype=float) - self.y0) / self.dy
        ny, nx = self.depths.shape
        fx = np.clip(fx, 0.0, nx - 1.0 - 1e-9)
        fy = np.clip(fy, 0.0, ny - 1.0 - 1e-9)
        j = np.floor(fx).astype(int)
        i = np.floor(fy).astype(int)
        return i, j, fx - j, fy - i

    def depth_at(self, x, y):
        i, j, tx, ty = self._local(x, y)
        d = self.depths
        v = (
            d[i, j] * (1 - tx) * (1 - ty)
            + d[i, j + 1] * tx * (1 - ty)
            + d[i + 1, j] * (1 - tx) * ty
            + d[i + 1, j + 1] * tx * ty
        )
        if np.ndim(x) == 0 and np.ndim(y) == 0:
            return float(v)
        return v

    def grad_at(self, x, y):
        i, j, tx, ty = self._local(x, y)
        d = self.depths
        gx = ((d[i, j + 1] - d[i, j]) * (1 - ty) + (d[i + 1, j + 1] - d[i + 1, j]) * ty) / self.dx
        gy = ((d[i + 1, j] - d[i, j]) * (1 - tx) + (d[i + 1, j + 1] - d[i, j + 1]) * tx) / self.dy
        if np.ndim(x) == 0 and np.ndim(y) == 0:
            return float(gx), float(gy)
        return gx, gy

    def x_axis(self) -> "np.ndarray":
        return self.x0 + self.dx * np.arange(self.depths.shape[1])

    def y_axis(self) -> "np.ndarray":
        return self.y0 + self.dy * np.arange(self.depths.shape[0])

    def to_dict(self) -> dict:
        return {
            "kind": "grid",
            "x0": self.x0,
            "y0": self.y0,
            "dx": self.dx,
            "dy": self.dy,
            "depths": self.depths.tolist(),
        }


def bathymetry_from_dict(cfg: dict) -> Bathymetry:
    kind = cfg.get("kind", "flat")
    if kind == "flat":
        return FlatBathymetry(float(cfg["depth_m"]))
    if kind == "slope":
        return PlanarSlopeBathymetry(float(cfg["depth0_m"]), float(cfg.get("gx", 0.0)), float(cfg.get("gy", 0.0)))
    if kind == "profile":
        return ProfileBathymetry(cfg["points"], float(cfg.get("azimuth_deg", 0.0)))
    if kind == "grid":
        return GridBathymetry(cfg["x0"], cfg["y0"], cfg["dx"], cfg["dy"], cfg["depths"])
    raise ValueError(f"Unknown bathymetry kind: {kind!r}")


def sample_grid(bathy: Bathymetry, x_range: Tuple[float, float], y_range: Tuple[float, float], n: int = 60) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
    """Sample any bathymetry to (x, y, Z) arrays for display."""
    xs = np.linspace(float(x_range[0]), float(x_range[1]), int(n))
    ys = np.linspace(float(y_range[0]), float(y_range[1]), int(n))
    X, Y = np.meshgrid(xs, ys)
    Z = -np.asarray(bathy.depth_at(X, Y), dtype=float)
    return xs, ys, Z
