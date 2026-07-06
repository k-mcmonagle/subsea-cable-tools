# -*- coding: utf-8 -*-
"""Backend-agnostic 3D scene description for the Cable Lay Simulator.

The solve controller builds a :class:`SceneData` from engine results; the
viewport (``view3d.py``) renders it. Nothing in this module imports Qt or
QGIS, so the contract is testable standalone.

Frame convention (matches the V3 engine): ``x, y`` horizontal metres in a
local/projected frame, ``z`` vertical metres with 0 at the sea surface and
negative down. Bed elevations are negative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


@dataclass
class CablePath:
    """A polyline of cable with per-vertex data for coloring and readouts.

    ``xyz`` is an (n, 3) float array. ``tension_kN`` and ``s_m`` (arc length
    from the top/first vertex) are length-n arrays or None. ``segment_index``
    maps each vertex to an assembly-segment index (for per-segment coloring);
    ``segment_colors`` maps those indices to '#rrggbb' strings. ``contact``
    optionally flags vertices resting on the seabed.
    """

    xyz: "np.ndarray"
    name: str = "cable"
    tension_kN: Optional["np.ndarray"] = None
    s_m: Optional["np.ndarray"] = None
    segment_index: Optional["np.ndarray"] = None
    segment_colors: Optional[List[str]] = None
    contact: Optional["np.ndarray"] = None
    color: str = "#1f77b4"          # fallback single color
    width: float = 2.0


@dataclass
class Marker:
    """A labelled point of interest (TDP, body, junction, anchor...)."""

    xyz: Tuple[float, float, float]
    label: str = ""
    kind: str = "point"             # 'tdp' | 'body' | 'anchor' | 'junction' | 'point'
    color: str = "#d62728"
    size: float = 6.0


@dataclass
class VesselGlyph:
    """Simple vessel representation at the water surface."""

    xy: Tuple[float, float]
    heading_deg: float = 0.0        # 0 = +x, counter-clockwise positive
    length_m: float = 60.0
    beam_m: float = 12.0
    label: str = "vessel"
    color: str = "#444444"


@dataclass
class BedGrid:
    """Seabed surface sampled on a regular grid.

    ``x`` (nx,), ``y`` (ny,) axis coordinates and ``z`` (ny, nx) bed
    elevations (negative down). The renderer may decimate for speed.
    """

    x: "np.ndarray"
    y: "np.ndarray"
    z: "np.ndarray"


@dataclass
class SceneData:
    """Everything the 3D viewport needs to draw one state."""

    bed: Optional[BedGrid] = None
    cables: List[CablePath] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    vessel: Optional[VesselGlyph] = None
    water_z: float = 0.0
    show_water_plane: bool = True
    title: str = ""
    # Preferred initial framing: ((xmin, xmax), (ymin, ymax), (zmin, zmax)).
    bounds: Optional[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]] = None

    def compute_bounds(self):
        """Bounds from content when not set explicitly."""
        if self.bounds is not None:
            return self.bounds
        xs, ys, zs = [], [], []
        if self.bed is not None:
            xs += [float(self.bed.x.min()), float(self.bed.x.max())]
            ys += [float(self.bed.y.min()), float(self.bed.y.max())]
            zs += [float(self.bed.z.min()), float(self.bed.z.max())]
        for c in self.cables:
            if c.xyz is not None and len(c.xyz):
                xs += [float(c.xyz[:, 0].min()), float(c.xyz[:, 0].max())]
                ys += [float(c.xyz[:, 1].min()), float(c.xyz[:, 1].max())]
                zs += [float(c.xyz[:, 2].min()), float(c.xyz[:, 2].max())]
        for m in self.markers:
            xs.append(float(m.xyz[0]))
            ys.append(float(m.xyz[1]))
            zs.append(float(m.xyz[2]))
        if self.vessel is not None:
            xs.append(float(self.vessel.xy[0]))
            ys.append(float(self.vessel.xy[1]))
            zs.append(0.0)
        if not xs:
            return ((-100.0, 100.0), (-100.0, 100.0), (-100.0, 0.0))
        zs.append(self.water_z)
        return (
            (min(xs), max(xs)),
            (min(ys), max(ys)),
            (min(zs), max(zs)),
        )
