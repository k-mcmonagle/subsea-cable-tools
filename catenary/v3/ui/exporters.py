# -*- coding: utf-8 -*-
"""File exporters for the Cable Lay Simulator (3D).

CSV tabulators for chain/snapshot/timeline results and minimal hand-built
ASCII DXF (R12-style) writers for the 3D scene and 2D profile views —
the same approach as the V2 dialog's DXF export, extended with true 3D
polylines and a layer table.

Pure Python + NumPy; no Qt/QGIS imports. Files are written with plain
``open()`` so the module is testable standalone.

Frame convention: ``x, y`` horizontal metres in the local frame, ``z``
vertical metres with 0 at the sea surface and negative down. Exported
``depth_m`` is positive down (``-z``).
"""

from __future__ import annotations

import csv
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

#: Per-node columns shared by :func:`chain_csv_rows` and :func:`snapshot_csv_rows`.
CHAIN_CSV_HEADER = [
    "s_m", "x_m", "y_m", "z_m", "depth_m", "tension_kN", "contact", "seg_id",
]


def _num(value: Any, ndigits: int = 3) -> Any:
    """Round a numeric value for CSV output; blank for non-finite/missing."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(f):
        return ""
    return round(f, ndigits)


def _at(arr: Optional["np.ndarray"], i: int) -> Any:
    """Element ``arr[i]`` clamped to the array end; blank when unavailable.

    Some per-chain arrays are per-element (n-1) rather than per-node (n) —
    e.g. ``seg_id`` — so the last node reuses the final element value.
    """
    if arr is None:
        return ""
    a = np.asarray(arr)
    if a.size == 0:
        return ""
    return a[min(i, a.size - 1)]


def chain_csv_rows(chain_snapshot) -> Tuple[List[str], List[List]]:
    """Header + rows for one chain: one row per node.

    Columns: s_m, x_m, y_m, z_m, depth_m (= -z), tension_kN,
    contact (0/1), seg_id.
    """
    xyz = np.asarray(chain_snapshot.xyz, dtype=float)
    rows: List[List] = []
    for i in range(len(xyz)):
        x, y, z = (float(v) for v in xyz[i])
        contact = _at(getattr(chain_snapshot, "contact", None), i)
        seg = _at(getattr(chain_snapshot, "seg_id", None), i)
        rows.append([
            _num(_at(getattr(chain_snapshot, "s", None), i)),
            _num(x), _num(y), _num(z), _num(-z),
            _num(_at(getattr(chain_snapshot, "tension_kN", None), i)),
            "" if contact == "" else int(bool(contact)),
            "" if seg == "" else int(seg),
        ])
    return list(CHAIN_CSV_HEADER), rows


def snapshot_csv_rows(snapshot) -> Tuple[List[str], List[List]]:
    """Header + rows for one snapshot: all chains concatenated.

    Adds a leading chain-name column plus the snapshot time ``t_s``.
    """
    header = ["chain", "t_s"] + list(CHAIN_CSV_HEADER)
    t = _num(getattr(snapshot, "t_s", 0.0))
    rows: List[List] = []
    for chain in snapshot.chains:
        _, chain_rows = chain_csv_rows(chain)
        for row in chain_rows:
            rows.append([chain.name, t] + row)
    return header, rows


def timeline_csv_rows(snapshots: Sequence) -> Tuple[List[str], List[List]]:
    """Header + rows summarising a run: one row per snapshot per chain.

    Junction positions are flattened as ``junction_<name>_x/y/z`` columns
    (union of junction names across all snapshots; blank when a junction is
    absent from a snapshot).
    """
    junction_names: List[str] = []
    seen = set()
    for snap in snapshots:
        for name in getattr(snap, "junction_xyz", {}) or {}:
            if name not in seen:
                seen.add(name)
                junction_names.append(name)

    header = [
        "t_s", "label", "chain", "length_m", "top_tension_kN",
        "end_tension_kN", "min_radius_m", "max_tension_kN", "pct_contact",
        "vessel_x", "vessel_y",
    ]
    for name in junction_names:
        header += [f"junction_{name}_x", f"junction_{name}_y", f"junction_{name}_z"]

    rows: List[List] = []
    for snap in snapshots:
        junctions = getattr(snap, "junction_xyz", {}) or {}
        jcols: List[Any] = []
        for name in junction_names:
            xyz = junctions.get(name)
            if xyz is None:
                jcols += ["", "", ""]
            else:
                jcols += [_num(xyz[0]), _num(xyz[1]), _num(xyz[2])]
        for chain in snap.chains:
            tension = np.asarray(chain.tension_kN, dtype=float)
            contact = np.asarray(chain.contact)
            max_tension = float(np.max(tension)) if tension.size else float("nan")
            pct_contact = (
                100.0 * float(np.count_nonzero(contact)) / contact.size
                if contact.size else float("nan")
            )
            rows.append([
                _num(snap.t_s), getattr(snap, "label", ""), chain.name,
                _num(chain.length_m), _num(chain.top_tension_kN),
                _num(chain.end_tension_kN), _num(chain.min_radius_m),
                _num(max_tension), _num(pct_contact, 1),
                _num(snap.vessel_xy[0]), _num(snap.vessel_xy[1]),
            ] + jcols)
    return header, rows


def schedule_csv_rows(snapshots: Sequence) -> Tuple[List[str], List[List]]:
    """Operational schedule sheet: one row per snapshot with the vessel
    state, the applied payout per line and the resulting tensions — the
    table a lay crew can follow (and check against) during the deployment."""
    chain_names: List[str] = []
    seen = set()
    for snap in snapshots:
        for c in snap.chains:
            if c.name not in seen:
                seen.add(c.name)
                chain_names.append(c.name)

    header = ["t_s", "phase", "vessel_x_m", "vessel_y_m", "heading_degN"]
    for name in chain_names:
        header.append(f"payout_{name}_mps")
    for name in chain_names:
        header.append(f"top_tension_{name}_kN")
    header += ["leg_imbalance_kN", "BU_x_m", "BU_y_m", "BU_z_m"]

    def _math_to_compass(deg: float) -> float:
        return (90.0 - float(deg)) % 360.0

    rows: List[List] = []
    for snap in snapshots:
        payout = getattr(snap, "payout_mps", {}) or {}
        by_name = {c.name: c for c in snap.chains}
        row: List[Any] = [
            _num(snap.t_s), getattr(snap, "label", ""),
            _num(snap.vessel_xy[0]), _num(snap.vessel_xy[1]),
            _num(_math_to_compass(snap.vessel_heading_deg), 1),
        ]
        for name in chain_names:
            row.append(_num(payout[name], 3) if name in payout else "")
        for name in chain_names:
            c = by_name.get(name)
            row.append(_num(c.top_tension_kN) if c is not None else "")
        c1, c2 = by_name.get("leg1"), by_name.get("leg2")
        row.append(_num(c1.top_tension_kN - c2.top_tension_kN)
                   if c1 is not None and c2 is not None else "")
        bu = (getattr(snap, "junction_xyz", {}) or {}).get("BU")
        row += ([_num(bu[0]), _num(bu[1]), _num(bu[2])] if bu is not None
                else ["", "", ""])
        rows.append(row)
    return header, rows


def write_csv(path: str, header: List[str], rows: List[List]) -> None:
    """Write header + rows to ``path`` as UTF-8 CSV."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# DXF (minimal hand-built ASCII, R12 style — matches the V2 writer approach)
# ---------------------------------------------------------------------------

# AutoCAD Color Index numbers for well-known layers and the cable cycle.
_ACI_SEABED = 8      # dark grey
_ACI_MARKERS = 1     # red
_ACI_VESSEL = 7      # white/black
_ACI_CABLE_CYCLE = [5, 3, 6, 4, 2, 30, 140, 200]  # blue, green, magenta, ...

_MAX_BED_WIRES = 40  # decimate bed grid to at most 40 x 40 wireframe nodes


def _sanitize_layer(name: str) -> str:
    """Conservative DXF layer naming: [A-Za-z0-9_-], max 31 chars."""
    raw = (name or "0").strip().replace(" ", "_")
    cleaned = "".join(ch for ch in raw if (ch.isalnum() or ch in ("_", "-")))
    return cleaned[:31] or "0"


def _fmt(v: float) -> str:
    return f"{float(v):.4f}"


def _pairs(*items: Tuple[int, Any]) -> str:
    """Serialise (group code, value) pairs as DXF text."""
    return "".join(f"{code}\n{value}\n" for code, value in items)


def _dxf_header_and_tables(layers: Sequence[Tuple[str, int]]) -> str:
    """Minimal HEADER plus LTYPE/LAYER tables declaring the given layers.

    ``layers`` is (name, ACI color) pairs; names must be pre-sanitized.
    """
    out = _pairs((0, "SECTION"), (2, "HEADER"),
                 (9, "$ACADVER"), (1, "AC1009"),
                 (0, "ENDSEC"))
    out += _pairs((0, "SECTION"), (2, "TABLES"))
    # CONTINUOUS linetype so layer records reference something defined.
    out += _pairs((0, "TABLE"), (2, "LTYPE"), (70, 1),
                  (0, "LTYPE"), (2, "CONTINUOUS"), (70, 64),
                  (3, "Solid line"), (72, 65), (73, 0), (40, "0.0"),
                  (0, "ENDTAB"))
    out += _pairs((0, "TABLE"), (2, "LAYER"), (70, len(layers) + 1))
    out += _pairs((0, "LAYER"), (2, "0"), (70, 0), (62, 7), (6, "CONTINUOUS"))
    for name, color in layers:
        out += _pairs((0, "LAYER"), (2, name), (70, 0),
                      (62, int(color)), (6, "CONTINUOUS"))
    out += _pairs((0, "ENDTAB"), (0, "ENDSEC"))
    return out


def _dxf_polyline3d(xyz: "np.ndarray", layer: str) -> str:
    """3D POLYLINE (flag 70=8) with 3D VERTEX records (flag 70=32)."""
    ent = _pairs((0, "POLYLINE"), (8, layer), (66, 1), (70, 8),
                 (10, "0.0"), (20, "0.0"), (30, "0.0"))
    for x, y, z in np.asarray(xyz, dtype=float):
        ent += _pairs((0, "VERTEX"), (8, layer), (70, 32),
                      (10, _fmt(x)), (20, _fmt(y)), (30, _fmt(z)))
    ent += _pairs((0, "SEQEND"), (8, layer))
    return ent


def _dxf_polyline2d(xy: Sequence[Tuple[float, float]], layer: str,
                    closed: bool = False) -> str:
    """2D POLYLINE at z=0 (flag 70=1 when closed)."""
    ent = _pairs((0, "POLYLINE"), (8, layer), (66, 1),
                 (70, 1 if closed else 0),
                 (10, "0.0"), (20, "0.0"), (30, "0.0"))
    for x, y in xy:
        ent += _pairs((0, "VERTEX"), (8, layer),
                      (10, _fmt(x)), (20, _fmt(y)), (30, "0.0"))
    ent += _pairs((0, "SEQEND"), (8, layer))
    return ent


def _dxf_line3d(p1, p2, layer: str) -> str:
    return _pairs((0, "LINE"), (8, layer),
                  (10, _fmt(p1[0])), (20, _fmt(p1[1])), (30, _fmt(p1[2])),
                  (11, _fmt(p2[0])), (21, _fmt(p2[1])), (31, _fmt(p2[2])))


def _dxf_point(xyz, layer: str) -> str:
    return _pairs((0, "POINT"), (8, layer),
                  (10, _fmt(xyz[0])), (20, _fmt(xyz[1])), (30, _fmt(xyz[2])))


def _dxf_text(xyz, text: str, height: float, layer: str) -> str:
    safe = (text or "").replace("\n", " ").replace("\r", " ")
    return _pairs((0, "TEXT"), (8, layer),
                  (10, _fmt(xyz[0])), (20, _fmt(xyz[1])), (30, _fmt(xyz[2])),
                  (40, _fmt(height)), (1, safe), (7, "STANDARD"))


def _dxf_document(layers: Sequence[Tuple[str, int]], entities: str) -> str:
    return (
        _dxf_header_and_tables(layers)
        + _pairs((0, "SECTION"), (2, "ENTITIES"))
        + entities
        + _pairs((0, "ENDSEC"), (0, "EOF"))
    )


def _decimate_indices(n: int, max_n: int) -> "np.ndarray":
    """At most ``max_n`` indices spanning ``range(n)``, endpoints included."""
    if n <= max_n:
        return np.arange(n)
    return np.unique(np.round(np.linspace(0, n - 1, max_n)).astype(int))


def _vessel_outline(vessel) -> List[Tuple[float, float]]:
    """Simple hull outline (pointed bow) in map/local xy at the surface."""
    half_l = 0.5 * float(vessel.length_m)
    half_b = 0.5 * float(vessel.beam_m)
    shoulder = 0.3 * float(vessel.length_m)  # bow taper starts here
    local = [
        (-half_l, -half_b),
        (shoulder, -half_b),
        (half_l, 0.0),
        (shoulder, half_b),
        (-half_l, half_b),
    ]
    a = math.radians(float(vessel.heading_deg))
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = float(vessel.xy[0]), float(vessel.xy[1])
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in local]


def scene_to_dxf_3d(scene, path: str) -> None:
    """Write a :class:`~..ui.scene.SceneData` as a minimal 3D ASCII DXF.

    * each cable: 3D POLYLINE on layer ``CABLE_<name>``;
    * markers: POINT + TEXT on layer ``MARKERS``;
    * bed grid: wireframe of LINE entities (decimated to at most 40 x 40)
      on layer ``SEABED``;
    * vessel: closed 2D polyline outline at z=0 on layer ``VESSEL``.
    """
    layers: List[Tuple[str, int]] = []
    layer_names = set()
    entities = ""

    def add_layer(name: str, color: int) -> str:
        base = _sanitize_layer(name)
        candidate = base
        k = 1
        while candidate in layer_names:
            suffix = f"_{k}"
            candidate = base[: 31 - len(suffix)] + suffix
            k += 1
        layer_names.add(candidate)
        layers.append((candidate, color))
        return candidate

    # Seabed wireframe.
    if scene.bed is not None:
        bed_layer = add_layer("SEABED", _ACI_SEABED)
        bx = np.asarray(scene.bed.x, dtype=float)
        by = np.asarray(scene.bed.y, dtype=float)
        bz = np.asarray(scene.bed.z, dtype=float)
        ji = _decimate_indices(bx.size, _MAX_BED_WIRES)
        ii = _decimate_indices(by.size, _MAX_BED_WIRES)
        # Lines along +x (within each kept row) and along +y (each kept col).
        for i in ii:
            for a, b in zip(ji[:-1], ji[1:]):
                entities += _dxf_line3d(
                    (bx[a], by[i], bz[i, a]), (bx[b], by[i], bz[i, b]), bed_layer)
        for j in ji:
            for a, b in zip(ii[:-1], ii[1:]):
                entities += _dxf_line3d(
                    (bx[j], by[a], bz[a, j]), (bx[j], by[b], bz[b, j]), bed_layer)

    # Cables.
    for k, cable in enumerate(scene.cables):
        if cable.xyz is None or len(cable.xyz) < 2:
            continue
        color = _ACI_CABLE_CYCLE[k % len(_ACI_CABLE_CYCLE)]
        layer = add_layer(f"CABLE_{cable.name}", color)
        entities += _dxf_polyline3d(cable.xyz, layer)

    # Markers.
    if scene.markers:
        marker_layer = add_layer("MARKERS", _ACI_MARKERS)
        bounds = scene.compute_bounds()
        span = max(bounds[0][1] - bounds[0][0], bounds[1][1] - bounds[1][0], 1.0)
        text_h = max(0.5, 0.01 * span)
        for m in scene.markers:
            entities += _dxf_point(m.xyz, marker_layer)
            if m.label:
                anchor = (m.xyz[0] + text_h, m.xyz[1] + text_h, m.xyz[2])
                entities += _dxf_text(anchor, m.label, text_h, marker_layer)

    # Vessel outline at the surface.
    if scene.vessel is not None:
        vessel_layer = add_layer("VESSEL", _ACI_VESSEL)
        entities += _dxf_polyline2d(_vessel_outline(scene.vessel),
                                    vessel_layer, closed=True)

    with open(path, "w", encoding="ascii", errors="replace", newline="\n") as fh:
        fh.write(_dxf_document(layers, entities))


def profile_to_dxf(polylines: Sequence[Tuple[str, "np.ndarray"]], path: str) -> None:
    """Write labelled 2D profile polylines as a minimal ASCII DXF.

    ``polylines`` is (label, (n, 2) array of (horizontal_m, z_m)) tuples;
    each label gets its own layer with a distinct color. Units metres.
    """
    layers: List[Tuple[str, int]] = []
    layer_names = set()
    entities = ""
    for k, (label, arr) in enumerate(polylines):
        pts = np.asarray(arr, dtype=float)
        if pts.ndim != 2 or pts.shape[1] != 2 or len(pts) < 2:
            raise ValueError(
                f"profile polyline {label!r} must be an (n, 2) array with n >= 2"
            )
        base = _sanitize_layer(label)
        candidate = base
        j = 1
        while candidate in layer_names:
            suffix = f"_{j}"
            candidate = base[: 31 - len(suffix)] + suffix
            j += 1
        layer_names.add(candidate)
        layers.append((candidate, _ACI_CABLE_CYCLE[k % len(_ACI_CABLE_CYCLE)]))
        entities += _dxf_polyline2d([(p[0], p[1]) for p in pts], candidate)
    with open(path, "w", encoding="ascii", errors="replace", newline="\n") as fh:
        fh.write(_dxf_document(layers, entities))
