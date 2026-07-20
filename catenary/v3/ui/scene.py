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
    """Parametric vessel at the water surface.

    ``xy`` is the plan position of the CABLE DEPARTURE POINT (chute) — the
    engine's top attachment — not the hull centre. The hull is placed around
    it via the CRP/chute offsets below, all in ship frame (x forward,
    positive ``*_stbd_m`` to starboard). ``height_m`` extrudes the hull from
    the waterline up to the deck (set it to the chute height so the drawn
    freeboard matches the configured chute height); 0 keeps the legacy flat
    outline.
    """

    xy: Tuple[float, float]
    heading_deg: float = 0.0        # 0 = +x, counter-clockwise positive (math frame)
    length_m: float = 60.0
    beam_m: float = 12.0
    height_m: float = 0.0           # waterline -> deck extrusion (chute height)
    crp_fwd_m: float = 0.0          # CRP forward of midship
    crp_stbd_m: float = 0.0         # CRP starboard of centreline
    chute_fwd_m: float = 0.0        # chute forward of CRP
    chute_stbd_m: float = 0.0       # chute starboard of CRP
    chute_radius_m: float = 0.0     # overboarding chute radius (drawn only)
    label: str = "vessel"
    # Text shown at the departure anchor ("chute"; "sheaves" for the
    # two-sheave BU scenes where xy is the sheave-pair centre).
    departure_label: str = "chute"
    color: str = "#444444"


def compass_to_math_deg(bearing_deg: float) -> float:
    """Compass bearing (deg clockwise from north) -> math angle (deg CCW
    from +x/east), for a local frame aligned with a projected map CRS."""
    return 90.0 - float(bearing_deg)


def math_to_compass_deg(math_deg: float) -> float:
    """Inverse of :func:`compass_to_math_deg`, normalised to [0, 360)."""
    return (90.0 - float(math_deg)) % 360.0


def _vessel_rotation(vessel: "VesselGlyph"):
    import math as _m

    h = _m.radians(float(getattr(vessel, "heading_deg", 0.0)))
    return _m.cos(h), _m.sin(h)


def vessel_footprint(vessel: "VesselGlyph") -> "np.ndarray":
    """(5, 2) waterline footprint in local/world plan coordinates.

    Ship frame: x forward, y to port (so starboard offsets enter negated);
    the polygon is anchored so the chute plan position lands on
    ``vessel.xy``.
    """
    length = max(float(getattr(vessel, "length_m", 60.0)), 1.0)
    beam = max(float(getattr(vessel, "beam_m", 12.0)), 0.5)
    rel = np.array([
        (length * 0.5, 0.0),
        (length * 0.15, beam * 0.5),
        (-length * 0.5, beam * 0.5),
        (-length * 0.5, -beam * 0.5),
        (length * 0.15, -beam * 0.5),
    ])
    # Chute position in ship frame relative to the hull centre.
    cx_ship = float(getattr(vessel, "crp_fwd_m", 0.0)) + float(getattr(vessel, "chute_fwd_m", 0.0))
    cy_ship = -(float(getattr(vessel, "crp_stbd_m", 0.0)) + float(getattr(vessel, "chute_stbd_m", 0.0)))
    rel = rel - np.array([cx_ship, cy_ship])
    c, s = _vessel_rotation(vessel)
    rot = rel @ np.array([[c, s], [-s, c]])
    return rot + np.asarray(vessel.xy, dtype=float)[:2]


def vessel_crp_xy(vessel: "VesselGlyph") -> Tuple[float, float]:
    """CRP plan position (world), derived back from the chute anchor."""
    rel = np.array([-float(getattr(vessel, "chute_fwd_m", 0.0)),
                    float(getattr(vessel, "chute_stbd_m", 0.0))])
    c, s = _vessel_rotation(vessel)
    out = rel @ np.array([[c, s], [-s, c]]) + np.asarray(vessel.xy, dtype=float)[:2]
    return float(out[0]), float(out[1])


def vessel_chute_xyz(vessel: "VesselGlyph", water_z: float = 0.0) -> Tuple[float, float, float]:
    """Chute (cable departure) point at deck level."""
    return (float(vessel.xy[0]), float(vessel.xy[1]),
            float(water_z) + float(getattr(vessel, "height_m", 0.0)))


def chute_arc_points(vessel: "VesselGlyph", water_z: float = 0.0, n: int = 12) -> Optional["np.ndarray"]:
    """Quarter-circle overboarding-chute arc (drawn geometry only).

    Starts at the chute top tangent to the deck, curving down on the aft
    side — mirroring the V2 catenary calculator's chute rendering. Returns
    (n, 3) points or None when no radius is set.
    """
    import math as _m

    r = float(getattr(vessel, "chute_radius_m", 0.0))
    if not (r > 0.0):
        return None
    c, s = _vessel_rotation(vessel)
    aft = np.array([-c, -s, 0.0])
    top = np.asarray(vessel_chute_xyz(vessel, water_z), dtype=float)
    centre = top - np.array([0.0, 0.0, r])
    th = np.linspace(0.0, _m.pi / 2.0, max(int(n), 2))
    return centre[None, :] + r * (np.sin(th)[:, None] * aft[None, :]
                                  + np.cos(th)[:, None] * np.array([0.0, 0.0, 1.0])[None, :])


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
