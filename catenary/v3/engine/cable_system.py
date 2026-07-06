# -*- coding: utf-8 -*-
"""Cable assemblies, chains and multi-chain systems for the 3D lay engine.

Pure Python + NumPy; no Qt/QGIS imports.

An **assembly** is the ordered list of cable segments and in-line bodies as
the user enters it (V2-compatible JSON, top-of-chute downward, with V3
extensions for hydrodynamics). A **chain** is a discretised run of cable —
per-element property arrays plus global node indices. A **system** is one or
more chains sharing a global node pool; chains may share end nodes
(junctions, e.g. a branching unit).

Property mapping direction:

* ``from_top`` — element properties looked up by arc distance from the chute
  end of the assembly (V2 static semantics).
* ``from_bottom`` — by arc distance from the bottom (last) end of the
  assembly. This keeps the material mapping stable while cable is paid out
  (the deployed window grows toward the top of the assembly), so timeline
  simulations use it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

G = 9.80665
RHO_SEAWATER = 1025.0


@dataclass
class SegmentSpec:
    """One cable type in the assembly (V2 segment + V3 hydro fields)."""

    name: str = "Cable"
    length_m: float = 0.0
    q_water_npm: float = 0.0          # submerged weight, N/m (negative = buoyant)
    q_air_npm: float = 0.0            # in-air weight, N/m (0 -> q_water used)
    friction_mu: Optional[float] = None
    bending_stiffness_kNm2: Optional[float] = None
    min_bend_radius_m: Optional[float] = None
    color: str = ""
    # --- V3 additions -----------------------------------------------------
    diameter_m: float = 0.0           # 0 -> hydro drag disabled for segment
    cd_normal: float = 1.2
    cd_tangential: float = 0.01
    mass_kgpm: float = 0.0            # physical mass/length; 0 -> derived


@dataclass
class BodySpec:
    """An in-line body (repeater, joint, BU...) as a point load."""

    name: str = "Body"
    point_load_kN: float = 0.0        # +down submerged weight, negative = buoyant
    color: str = ""
    # --- V3 additions -----------------------------------------------------
    cda_m2: float = 0.0               # lumped drag area Cd*A for body drag


AssemblyItem = Union[SegmentSpec, BodySpec]


def parse_assembly(data: Sequence[dict]) -> List[AssemblyItem]:
    """Parse the V2-compatible assembly JSON list (with V3 extras)."""
    items: List[AssemblyItem] = []
    for entry in data or []:
        kind = str(entry.get("type", "segment")).strip().lower()
        if kind == "body":
            items.append(
                BodySpec(
                    name=str(entry.get("name", "Body")),
                    point_load_kN=float(entry.get("point_load_kN", 0.0) or 0.0),
                    color=str(entry.get("color", "") or ""),
                    cda_m2=float(entry.get("cda_m2", 0.0) or 0.0),
                )
            )
        else:
            items.append(
                SegmentSpec(
                    name=str(entry.get("name", "Cable")),
                    length_m=float(entry.get("length_m", 0.0) or 0.0),
                    q_water_npm=float(entry.get("q_water_npm", 0.0) or 0.0),
                    q_air_npm=float(entry.get("q_air_npm", 0.0) or 0.0),
                    friction_mu=_opt_float(entry.get("friction_mu")),
                    bending_stiffness_kNm2=_opt_float(entry.get("bending_stiffness_kNm2")),
                    min_bend_radius_m=_opt_float(entry.get("min_bend_radius_m")),
                    color=str(entry.get("color", "") or ""),
                    diameter_m=float(entry.get("diameter_m", 0.0) or 0.0),
                    cd_normal=float(entry.get("cd_normal", 1.2) or 1.2),
                    cd_tangential=float(entry.get("cd_tangential", 0.01) or 0.0),
                    mass_kgpm=float(entry.get("mass_kgpm", 0.0) or 0.0),
                )
            )
    return items


def assembly_to_json_data(items: Sequence[AssemblyItem]) -> List[dict]:
    out: List[dict] = []
    for it in items:
        if isinstance(it, BodySpec):
            d = {"type": "body", "name": it.name, "point_load_kN": it.point_load_kN}
            if it.color:
                d["color"] = it.color
            if it.cda_m2:
                d["cda_m2"] = it.cda_m2
        else:
            d = {
                "type": "segment",
                "name": it.name,
                "length_m": it.length_m,
                "q_water_npm": it.q_water_npm,
                "q_air_npm": it.q_air_npm,
            }
            if it.friction_mu is not None:
                d["friction_mu"] = it.friction_mu
            if it.bending_stiffness_kNm2 is not None:
                d["bending_stiffness_kNm2"] = it.bending_stiffness_kNm2
            if it.min_bend_radius_m is not None:
                d["min_bend_radius_m"] = it.min_bend_radius_m
            if it.color:
                d["color"] = it.color
            if it.diameter_m:
                d["diameter_m"] = it.diameter_m
            d["cd_normal"] = it.cd_normal
            d["cd_tangential"] = it.cd_tangential
            if it.mass_kgpm:
                d["mass_kgpm"] = it.mass_kgpm
        out.append(d)
    return out


def _opt_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def uniform_assembly(length_m: float, q_water_npm: float, *, q_air_npm: float = 0.0,
                     diameter_m: float = 0.0, cd_normal: float = 1.2,
                     cd_tangential: float = 0.01, mu: float = 0.3,
                     EI_kNm2: float = 0.0, name: str = "Cable") -> List[AssemblyItem]:
    """Convenience single-segment assembly."""
    return [
        SegmentSpec(
            name=name,
            length_m=float(length_m),
            q_water_npm=float(q_water_npm),
            q_air_npm=float(q_air_npm),
            friction_mu=float(mu),
            bending_stiffness_kNm2=float(EI_kNm2),
            diameter_m=float(diameter_m),
            cd_normal=float(cd_normal),
            cd_tangential=float(cd_tangential),
        )
    ]


@dataclass
class Defaults:
    """Fallbacks for blank per-segment values."""

    q_water_npm: float = 200.0
    q_air_npm: float = 0.0
    mu: float = 0.3
    EI_Nm2: float = 0.0
    mbr_m: float = 0.0
    diameter_m: float = 0.0
    cd_normal: float = 1.2
    cd_tangential: float = 0.01


class AssemblyMapper:
    """Samples assembly properties at arc positions.

    ``direction`` is 'from_top' (positions measured from the first item) or
    'from_bottom' (from the end of the last item, i.e. material-stable under
    pay-out at the top).
    """

    def __init__(self, items: Sequence[AssemblyItem], defaults: Optional[Defaults] = None,
                 direction: str = "from_top"):
        if direction not in ("from_top", "from_bottom"):
            raise ValueError("direction must be 'from_top' or 'from_bottom'")
        self.items = list(items)
        self.defaults = defaults or Defaults()
        self.direction = direction
        # Segment boundaries in from-top coordinates, plus body positions.
        self._segments: List[Tuple[float, float, SegmentSpec, int]] = []
        self._bodies: List[Tuple[float, BodySpec]] = []
        s = 0.0
        seg_index = 0
        for it in self.items:
            if isinstance(it, SegmentSpec):
                if it.length_m > 0:
                    self._segments.append((s, s + it.length_m, it, seg_index))
                    s += it.length_m
                seg_index += 1
            else:
                self._bodies.append((s, it))
                seg_index += 1
        self.total_length_m = s

    def _to_top_coords(self, m: "np.ndarray") -> "np.ndarray":
        if self.direction == "from_top":
            return m
        return self.total_length_m - m

    def element_arrays(self, s_elem_mid: "np.ndarray") -> Dict[str, "np.ndarray"]:
        """Per-element property arrays at the given arc midpoints.

        Positions beyond the assembly extent are clamped to the nearest end
        segment (with a 'clamped' flag so callers can warn).
        """
        d = self.defaults
        pos = self._to_top_coords(np.asarray(s_elem_mid, dtype=float))
        n = len(pos)
        out = {
            "qw": np.full(n, float(d.q_water_npm)),
            "qa": np.full(n, float(d.q_air_npm)),
            "mu": np.full(n, float(d.mu)),
            "EI": np.full(n, float(d.EI_Nm2)),
            "mbr": np.full(n, float(d.mbr_m)),
            "dia": np.full(n, float(d.diameter_m)),
            "cdn": np.full(n, float(d.cd_normal)),
            "cdt": np.full(n, float(d.cd_tangential)),
            "rho_kgpm": np.zeros(n),
            "seg_id": np.full(n, -1, dtype=int),
            "clamped": np.zeros(n, dtype=bool),
        }
        if not self._segments:
            return out
        starts = np.array([a for a, _, _, _ in self._segments])
        ends = np.array([b for _, b, _, _ in self._segments])
        clamped_lo = pos < starts[0]
        clamped_hi = pos > ends[-1]
        out["clamped"] = clamped_lo | clamped_hi
        pc = np.clip(pos, starts[0], ends[-1] - 1e-9)
        idx = np.clip(np.searchsorted(ends, pc, side="right"), 0, len(self._segments) - 1)
        for k, (a, b, seg, seg_index) in enumerate(self._segments):
            sel = idx == k
            if not np.any(sel):
                continue
            out["qw"][sel] = seg.q_water_npm if seg.q_water_npm != 0.0 else d.q_water_npm
            qa = seg.q_air_npm if seg.q_air_npm != 0.0 else (d.q_air_npm or 0.0)
            out["qa"][sel] = qa
            out["mu"][sel] = seg.friction_mu if seg.friction_mu is not None else d.mu
            EI = seg.bending_stiffness_kNm2
            out["EI"][sel] = (EI * 1000.0) if EI is not None else d.EI_Nm2
            out["mbr"][sel] = seg.min_bend_radius_m if seg.min_bend_radius_m is not None else d.mbr_m
            out["dia"][sel] = seg.diameter_m if seg.diameter_m > 0 else d.diameter_m
            out["cdn"][sel] = seg.cd_normal
            out["cdt"][sel] = seg.cd_tangential
            qw_eff = out["qw"][sel]
            if seg.mass_kgpm > 0:
                out["rho_kgpm"][sel] = seg.mass_kgpm
            else:
                # Estimate physical mass/length from in-air weight when given,
                # else from submerged weight + displaced water.
                dia = out["dia"][sel]
                disp = RHO_SEAWATER * math.pi / 4.0 * dia ** 2
                out["rho_kgpm"][sel] = np.where(
                    out["qa"][sel] > 0.0, out["qa"][sel] / G, np.maximum(qw_eff / G + disp, 0.1)
                )
            out["seg_id"][sel] = seg_index
        return out

    def bodies_in_window(self, m_lo: float, m_hi: float) -> List[Tuple[float, BodySpec]]:
        """Bodies whose arc position (in the mapper's direction) lies in
        [m_lo, m_hi]; returns (m_position, body)."""
        out: List[Tuple[float, BodySpec]] = []
        for s_top, body in self._bodies:
            m = s_top if self.direction == "from_top" else self.total_length_m - s_top
            if m_lo - 1e-9 <= m <= m_hi + 1e-9:
                out.append((m, body))
        return out

    def segment_name(self, seg_index: int) -> str:
        for it_index, it in enumerate(self.items):
            if it_index == seg_index and isinstance(it, SegmentSpec):
                return it.name
        return f"segment {seg_index}"

    def segment_colors(self) -> Dict[int, str]:
        return {
            i: (it.color or "")
            for i, it in enumerate(self.items)
            if isinstance(it, SegmentSpec)
        }


@dataclass
class Chain:
    """A discretised cable run: global node indices + per-element arrays."""

    name: str
    idx: "np.ndarray"                  # (n_elems+1,) global node ids
    L0: "np.ndarray"                   # (n_elems,) rest lengths, m
    qw: "np.ndarray"                   # N/m submerged
    qa: "np.ndarray"                   # N/m in air (0 -> qw used above water)
    dia: "np.ndarray"                  # m
    cdn: "np.ndarray"
    cdt: "np.ndarray"
    EI: "np.ndarray"                   # N.m^2
    mu: "np.ndarray"
    mbr: "np.ndarray"                  # m (0 = unspecified)
    rho_kgpm: "np.ndarray"             # physical mass per length
    seg_id: "np.ndarray"               # assembly segment index per element
    transport_speed_mps: float = 0.0   # material speed along the chain (s+)

    @property
    def n_elems(self) -> int:
        return len(self.L0)

    @property
    def length_m(self) -> float:
        return float(np.sum(self.L0))

    def s_nodes(self) -> "np.ndarray":
        return np.concatenate([[0.0], np.cumsum(self.L0)])


@dataclass
class CableSystem:
    """Global node pool + chains + lumped nodal loads."""

    X: "np.ndarray"                    # (n_nodes, 3) positions (mutable state)
    chains: List[Chain] = field(default_factory=list)
    fixed: "np.ndarray" = None         # (n_nodes,) bool
    point_force_N: "np.ndarray" = None  # (n_nodes, 3) static extra force
    body_cda_m2: "np.ndarray" = None   # (n_nodes,) lumped drag area
    node_labels: Dict[int, str] = field(default_factory=dict)

    def __post_init__(self):
        n = len(self.X)
        if self.fixed is None:
            self.fixed = np.zeros(n, dtype=bool)
        if self.point_force_N is None:
            self.point_force_N = np.zeros((n, 3))
        if self.body_cda_m2 is None:
            self.body_cda_m2 = np.zeros(n)

    @property
    def n_nodes(self) -> int:
        return len(self.X)


class SystemBuilder:
    """Incrementally builds a CableSystem with shared junction nodes."""

    def __init__(self):
        self._pos: List[Tuple[float, float, float]] = []
        self._fixed: List[bool] = []
        self._chains: List[Chain] = []
        self._pf: List[Tuple[int, Tuple[float, float, float]]] = []
        self._cda: List[Tuple[int, float]] = []
        self._labels: Dict[int, str] = {}

    def add_node(self, xyz: Sequence[float], fixed: bool = False, label: str = "") -> int:
        self._pos.append((float(xyz[0]), float(xyz[1]), float(xyz[2])))
        self._fixed.append(bool(fixed))
        i = len(self._pos) - 1
        if label:
            self._labels[i] = label
        return i

    def set_fixed(self, node: int, fixed: bool = True):
        self._fixed[node] = bool(fixed)

    def add_point_force(self, node: int, force_N: Sequence[float]):
        self._pf.append((node, (float(force_N[0]), float(force_N[1]), float(force_N[2]))))

    def add_body_drag(self, node: int, cda_m2: float):
        self._cda.append((node, float(cda_m2)))

    def add_chain(
        self,
        name: str,
        mapper: AssemblyMapper,
        length_m: float,
        n_elems: int,
        shape_xyz: "np.ndarray",
        *,
        start_node: Optional[int] = None,
        end_node: Optional[int] = None,
        window_lo_m: float = 0.0,
        transport_speed_mps: float = 0.0,
        add_bodies: bool = True,
    ) -> Chain:
        """Add a chain of ``length_m`` discretised into ``n_elems`` elements.

        ``shape_xyz`` is an (n_elems+1, 3) initial geometry. Properties are
        sampled from ``mapper`` over the arc window
        ``[window_lo_m, window_lo_m + length_m]`` (in the mapper's own
        direction convention); element k spans ``window_lo_m + s_k``.
        ``start_node``/``end_node`` reuse existing node ids (junctions);
        interior nodes are always new.
        """
        if n_elems < 2:
            raise ValueError("n_elems must be >= 2")
        shape_xyz = np.asarray(shape_xyz, dtype=float)
        if shape_xyz.shape != (n_elems + 1, 3):
            raise ValueError(f"shape_xyz must be ({n_elems + 1}, 3)")
        idx = np.empty(n_elems + 1, dtype=int)
        for k in range(n_elems + 1):
            if k == 0 and start_node is not None:
                idx[k] = start_node
            elif k == n_elems and end_node is not None:
                idx[k] = end_node
            else:
                idx[k] = self.add_node(shape_xyz[k])
        L0 = np.full(n_elems, float(length_m) / n_elems)
        ds = float(length_m) / n_elems
        s_top = (np.arange(n_elems) + 0.5) * ds  # element mids from node 0 (top)
        if mapper.direction == "from_bottom":
            # Mapper positions are distances from the bottom (last) end.
            s_mid = window_lo_m + (float(length_m) - s_top)
        else:
            s_mid = window_lo_m + s_top
        props = mapper.element_arrays(s_mid)
        chain = Chain(
            name=name,
            idx=idx,
            L0=L0,
            qw=props["qw"],
            qa=props["qa"],
            dia=props["dia"],
            cdn=props["cdn"],
            cdt=props["cdt"],
            EI=props["EI"],
            mu=props["mu"],
            mbr=props["mbr"],
            rho_kgpm=props["rho_kgpm"],
            seg_id=props["seg_id"],
            transport_speed_mps=float(transport_speed_mps),
        )
        self._chains.append(chain)
        if add_bodies:
            ds = float(length_m) / n_elems
            for m_pos, body in mapper.bodies_in_window(window_lo_m, window_lo_m + length_m):
                k = int(round((m_pos - window_lo_m) / ds))
                if mapper.direction == "from_bottom":
                    # m_pos is measured from the bottom end; node 0 is the top.
                    k = n_elems - k
                k = max(0, min(n_elems, k))
                node = int(idx[k])
                self.add_point_force(node, (0.0, 0.0, -body.point_load_kN * 1000.0))
                if body.cda_m2:
                    self.add_body_drag(node, body.cda_m2)
                if body.name:
                    self._labels.setdefault(node, body.name)
        return chain

    def build(self) -> CableSystem:
        X = np.asarray(self._pos, dtype=float)
        sysm = CableSystem(
            X=X,
            chains=list(self._chains),
            fixed=np.asarray(self._fixed, dtype=bool),
        )
        for node, f in self._pf:
            sysm.point_force_N[node] += np.asarray(f)
        for node, a in self._cda:
            sysm.body_cda_m2[node] += a
        sysm.node_labels = dict(self._labels)
        return sysm


def straight_shape(p0: Sequence[float], p1: Sequence[float], n_elems: int) -> "np.ndarray":
    """(n+1, 3) points evenly spaced on the straight line p0 -> p1."""
    t = np.linspace(0.0, 1.0, n_elems + 1)[:, None]
    a = np.asarray(p0, dtype=float)[None, :]
    b = np.asarray(p1, dtype=float)[None, :]
    return a + (b - a) * t


def sagged_shape(p0: Sequence[float], p1: Sequence[float], n_elems: int, slack_frac: float = 0.05) -> "np.ndarray":
    """Straight line with a parabolic vertical sag — a benign DR seed for a
    chain of length slightly greater than the chord."""
    pts = straight_shape(p0, p1, n_elems)
    t = np.linspace(0.0, 1.0, n_elems + 1)
    chord = float(np.linalg.norm(np.asarray(p1, dtype=float) - np.asarray(p0, dtype=float)))
    sag = max(0.5, 1.5 * slack_frac * chord)
    pts[:, 2] -= sag * 4.0 * t * (1.0 - t)
    return pts


def resample_polyline(xyz: "np.ndarray", n_out: int) -> "np.ndarray":
    """Resample a polyline to n_out+1 points, uniform in arc length."""
    xyz = np.asarray(xyz, dtype=float)
    seg = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s[-1])
    if total <= 0:
        return np.repeat(xyz[:1], n_out + 1, axis=0)
    st = np.linspace(0.0, total, n_out + 1)
    out = np.empty((n_out + 1, 3))
    for c in range(3):
        out[:, c] = np.interp(st, s, xyz[:, c])
    return out
