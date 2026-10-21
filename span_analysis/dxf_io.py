# -*- coding: utf-8 -*-
"""DXF for Span & Slack Analysis: read a seabed profile or a feature shape, write the scene.

Reading takes ASCII DXF lines and polylines (LINE, LWPOLYLINE, POLYLINE /
VERTEX) by layer. A profile drawing gives chainage or KP along X and
elevation or depth along Y, often with a vertical exaggeration; a feature
drawing gives a cross-section along the route (a rock berm, a crossing).

Writing gives a DXF R12 (AC1009) file in metres: X is KP x 1000, Y is the
elevation times the vertical exaggeration. Each item has its own layer
(``SA_SEABED``, ``SA_CABLE``, ``SA_SPAN_RED`` ...), and the tension, slack and
slope graphs sit in panels below the profile. Pure Python + numpy: no QGIS,
no extra packages.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
X_CHAINAGE_M = "chainage_m"
X_KP_KM = "kp_km"
X_KP_M = "kp_m"
X_CHOICES = ((X_CHAINAGE_M, "Chainage (m) from the start KP"), (X_KP_KM, "KP (km)"), (X_KP_M, "KP (m)"))
Y_ELEVATION = "elevation"
Y_DEPTH = "depth"
Y_CHOICES = ((Y_ELEVATION, "Elevation (m, up)"), (Y_DEPTH, "Depth (m, down)"))


def _pairs_from_text(text: str) -> List[Tuple[int, str]]:
    lines = text.splitlines()
    out = []
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip())
        except ValueError:
            continue
        out.append((code, lines[i + 1].strip()))
    return out


def read_polylines(path: str) -> Dict[str, List[np.ndarray]]:
    """``{layer: [array (n, 2) of x, y], ...}`` from the ENTITIES section of an ASCII DXF."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    if "Binary DXF" in text[:32]:      # the binary DXF file header
        raise ValueError("Binary DXF is not supported: save the drawing as ASCII DXF.")
    pairs = _pairs_from_text(text)
    out: Dict[str, List[np.ndarray]] = {}
    in_entities = False
    i = 0
    n = len(pairs)

    def add(layer, points):
        if len(points) >= 2:
            out.setdefault(layer or "0", []).append(np.asarray(points, dtype=float))

    while i < n:
        code, value = pairs[i]
        if code == 0 and value == "SECTION" and i + 1 < n and pairs[i + 1] == (2, "ENTITIES"):
            in_entities = True
            i += 2
            continue
        if code == 0 and value == "ENDSEC":
            in_entities = False
        if not in_entities or code != 0:
            i += 1
            continue
        kind = value
        i += 1
        attrs: List[Tuple[int, str]] = []
        while i < n and pairs[i][0] != 0:
            attrs.append(pairs[i])
            i += 1
        layer = next((v for c, v in attrs if c == 8), "0")
        if kind == "LINE":
            values = {c: v for c, v in attrs}
            try:
                add(layer, [(float(values[10]), float(values[20])), (float(values[11]), float(values[21]))])
            except (KeyError, ValueError):
                pass
        elif kind == "LWPOLYLINE":
            xs = [float(v) for c, v in attrs if c == 10]
            ys = [float(v) for c, v in attrs if c == 20]
            add(layer, list(zip(xs, ys)))
        elif kind == "POLYLINE":
            points = []
            while i < n:
                code, value = pairs[i]
                if code == 0 and value == "VERTEX":
                    i += 1
                    vattrs = {}
                    while i < n and pairs[i][0] != 0:
                        vattrs.setdefault(pairs[i][0], pairs[i][1])
                        i += 1
                    try:
                        points.append((float(vattrs[10]), float(vattrs[20])))
                    except (KeyError, ValueError):
                        pass
                    continue
                if code == 0 and value == "SEQEND":
                    i += 1
                    while i < n and pairs[i][0] != 0:
                        i += 1
                break
            add(layer, points)
    if not out:
        raise ValueError("The DXF holds no lines or polylines.")
    return out


def merged_points(polylines: Sequence[np.ndarray]) -> np.ndarray:
    """All vertices of the polylines, sorted by x, repeated x averaged."""
    points = np.vstack([np.asarray(p, dtype=float) for p in polylines])
    order = np.argsort(points[:, 0], kind="stable")
    points = points[order]
    xs, index = np.unique(np.round(points[:, 0], 6), return_inverse=True)
    ys = np.bincount(index, weights=points[:, 1]) / np.bincount(index)
    return np.column_stack((xs, ys))


@dataclass
class ProfileDxfOptions:
    layer: str
    x_mode: str = X_CHAINAGE_M
    start_kp: float = 0.0
    y_mode: str = Y_ELEVATION
    vertical_exaggeration: float = 1.0
    y_offset_m: float = 0.0       # added to the elevation after the exaggeration is removed


def profile_from_dxf(path: str, options: ProfileDxfOptions) -> Dict:
    """A seabed profile (plain lists, as the dialog stores profiles) from one DXF layer."""
    layers = read_polylines(path)
    if options.layer not in layers:
        raise ValueError(f"The DXF has no lines on layer '{options.layer}'.")
    points = merged_points(layers[options.layer])
    if len(points) < 3:
        raise ValueError("The DXF layer has fewer than three profile points.")
    x, y = points[:, 0], points[:, 1]
    if options.x_mode == X_KP_KM:
        kp = x
    elif options.x_mode == X_KP_M:
        kp = x / 1000.0
    else:
        kp = options.start_kp + (x - x[0]) / 1000.0
    ve = options.vertical_exaggeration if options.vertical_exaggeration > 0 else 1.0
    value = y / ve
    depth = -value - options.y_offset_m if options.y_mode == Y_ELEVATION else value + options.y_offset_m
    chainage = (kp - kp[0]) * 1000.0
    notes = [f"Profile from DXF layer '{options.layer}': {len(kp):,} points, KP {kp[0]:.3f} to {kp[-1]:.3f}"
             + (f", vertical exaggeration {ve:g} removed" if ve != 1.0 else "") + "."]
    return {"x_m": chainage.tolist(), "kp_km": kp.tolist(), "depth_m": depth.tolist(), "notes": notes,
            "source": "DXF file"}


def shape_from_dxf(path: str, layer: str, scale: float = 1.0,
                   vertical_exaggeration: float = 1.0) -> List[Tuple[float, float]]:
    """A feature cross-section: ``(offset from its middle m, height above its end-to-end base m)``.

    The base is the straight line between the shape's first and last points,
    so a berm drawn on a sloping seabed gives its height above that seabed.
    """
    layers = read_polylines(path)
    if layer not in layers:
        raise ValueError(f"The DXF has no lines on layer '{layer}'.")
    points = merged_points(layers[layer])
    if len(points) < 2:
        raise ValueError("The DXF layer has fewer than two points.")
    s = scale if scale > 0 else 1.0
    ve = vertical_exaggeration if vertical_exaggeration > 0 else 1.0
    x = points[:, 0] * s
    y = points[:, 1] * s / ve
    base = y[0] + (y[-1] - y[0]) * (x - x[0]) / (x[-1] - x[0]) if x[-1] > x[0] else np.full(len(x), y[0])
    middle = 0.5 * (x[0] + x[-1])
    return [(float(a - middle), float(b)) for a, b in zip(x, y - base)]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _fmt(value: float) -> str:
    return f"{float(value):.4f}"


def _pairs(*items) -> str:
    return "".join(f"{code}\n{value}\n" for code, value in items)


def _clean_layer(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name.upper())[:31] or "0"


class DxfWriter:
    """A minimal DXF R12 writer: layers, polylines, lines, points and text."""

    def __init__(self):
        self.layers: Dict[str, int] = {}
        self.parts: List[str] = []

    def layer(self, name: str, colour: int) -> str:
        clean = _clean_layer(name)
        self.layers.setdefault(clean, int(colour))
        return clean

    def polyline(self, xy, layer: str, closed: bool = False) -> None:
        points = [(float(x), float(y)) for x, y in xy if math.isfinite(x) and math.isfinite(y)]
        if len(points) < 2:
            return
        out = _pairs((0, "POLYLINE"), (8, layer), (66, 1), (70, 1 if closed else 0),
                     (10, "0.0"), (20, "0.0"), (30, "0.0"))
        for x, y in points:
            out += _pairs((0, "VERTEX"), (8, layer), (10, _fmt(x)), (20, _fmt(y)), (30, "0.0"))
        out += _pairs((0, "SEQEND"), (8, layer))
        self.parts.append(out)

    def line(self, x1, y1, x2, y2, layer: str) -> None:
        self.parts.append(_pairs((0, "LINE"), (8, layer), (10, _fmt(x1)), (20, _fmt(y1)), (30, "0.0"),
                                 (11, _fmt(x2)), (21, _fmt(y2)), (31, "0.0")))

    def point(self, x, y, layer: str) -> None:
        self.parts.append(_pairs((0, "POINT"), (8, layer), (10, _fmt(x)), (20, _fmt(y)), (30, "0.0")))

    def text(self, x, y, text: str, height: float, layer: str, align: int = 0) -> None:
        safe = str(text or "").replace("\n", " ").replace("\r", " ")
        items = [(0, "TEXT"), (8, layer), (10, _fmt(x)), (20, _fmt(y)), (30, "0.0"), (40, _fmt(height)),
                 (1, safe)]
        if align:
            items += [(72, align), (11, _fmt(x)), (21, _fmt(y)), (31, "0.0")]
        self.parts.append(_pairs(*items))

    def document(self) -> str:
        head = _pairs((0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, "AC1009"), (0, "ENDSEC"))
        head += _pairs((0, "SECTION"), (2, "TABLES"))
        head += _pairs((0, "TABLE"), (2, "LTYPE"), (70, 1), (0, "LTYPE"), (2, "CONTINUOUS"), (70, 64),
                       (3, "Solid line"), (72, 65), (73, 0), (40, "0.0"), (0, "ENDTAB"))
        head += _pairs((0, "TABLE"), (2, "LAYER"), (70, len(self.layers) + 1))
        head += _pairs((0, "LAYER"), (2, "0"), (70, 0), (62, 7), (6, "CONTINUOUS"))
        for name, colour in self.layers.items():
            head += _pairs((0, "LAYER"), (2, name), (70, 0), (62, colour), (6, "CONTINUOUS"))
        head += _pairs((0, "ENDTAB"), (0, "ENDSEC"))
        return head + _pairs((0, "SECTION"), (2, "ENTITIES")) + "".join(self.parts) + _pairs((0, "ENDSEC"),
                                                                                              (0, "EOF"))


@dataclass
class SceneOptions:
    kp_window: Optional[Tuple[float, float]] = None
    vertical_exaggeration: float = 1.0
    graphs: bool = True
    text_height_m: float = 0.0       # 0: auto (1% of the window length)
    title: str = ""
    max_vertices: int = 20000
    slack_basis: str = "seabed"      # the slack graph over the seabed (bottom slack) or over the route


def _nice_step(span: float, target: int = 10) -> float:
    raw = span / max(target, 1)
    if raw <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _thin(n: int, limit: int) -> np.ndarray:
    if n <= limit:
        return np.arange(n)
    return np.unique(np.round(np.linspace(0, n - 1, limit)).astype(int))


def export_scene(result, path: str, options: SceneOptions, detail=None) -> Dict[str, int]:
    """Write the profile, spans, supports, bodies, features and graphs to a DXF file.

    Returns the layers written ``{name: ACI colour}``.
    """
    st = result.stations
    kp = st.kp_km
    lo, hi = (float(kp[0]), float(kp[-1])) if options.kp_window is None else sorted(options.kp_window)
    sel = (kp >= lo - 1e-9) & (kp <= hi + 1e-9)
    if sel.sum() < 2:
        raise ValueError("The KP window holds fewer than two stations.")
    idx = np.nonzero(sel)[0]
    i0, i1 = int(idx[0]), int(idx[-1])
    ve = options.vertical_exaggeration if options.vertical_exaggeration > 0 else 1.0
    x_all = kp * 1000.0
    length = float(x_all[i1] - x_all[i0])
    th = options.text_height_m if options.text_height_m > 0 else max(length * 0.008, 0.2)
    w = DxfWriter()
    names = {
        "seabed": w.layer("SA_SEABED", 34), "sampled": w.layer("SA_SEABED_SAMPLED", 8),
        "cable": w.layer("SA_CABLE", 5), "detail": w.layer("SA_CABLE_DETAIL", 6),
        "red": w.layer("SA_SPAN_RED", 1), "amber": w.layer("SA_SPAN_AMBER", 30),
        "indicative": w.layer("SA_SPAN_INDICATIVE", 5),
        "clear": w.layer("SA_CLEAR_UNDER_LIMITS", 9), "supports": w.layer("SA_SUPPORTS", 7),
        "bodies": w.layer("SA_BODIES", 2), "features": w.layer("SA_FEATURES", 40),
        "sea": w.layer("SA_SEA_LEVEL", 4), "frame": w.layer("SA_FRAME", 7), "text": w.layer("SA_TEXT", 7),
        "protection": w.layer("SA_PROTECTION", 140),
    }
    keep = idx[_thin(len(idx), options.max_vertices)]

    def y_of(elev):
        return np.asarray(elev, dtype=float) * ve

    seabed = st.obstacle_m
    w.polyline(zip(x_all[keep], y_of(seabed[keep])), names["seabed"])
    if st.filter_m > 0 or bool(np.any(st.feature_m != 0)):
        w.polyline(zip(x_all[keep], y_of(st.raw_elev_m[keep])), names["sampled"])
    w.polyline(zip(x_all[keep], y_of(result.cable_m[keep])), names["cable"])
    if detail is not None:
        dk = detail.kp_km * 1000.0
        dsel = (detail.kp_km >= lo) & (detail.kp_km <= hi)
        dkeep = np.nonzero(dsel)[0]
        dkeep = dkeep[_thin(len(dkeep), options.max_vertices)]
        w.polyline(zip(dk[dkeep], y_of(detail.beam.y_m[dkeep])), names["detail"])

    # Spans: closed outlines (cable above, seabed below), by level, and labels.
    from ..cable_seabed import planning as pl
    from ..cable_seabed.core import runs

    reported = np.zeros(st.n, dtype=bool)
    for k, span in enumerate(result.spans):
        reported[span.i0:span.i1 + 1] = True
        if span.i1 < i0 or span.i0 > i1:
            continue
        a, b = max(span.i0, i0), min(span.i1, i1)
        ring = list(zip(x_all[a:b + 1], y_of(result.cable_m[a:b + 1]))) + \
            list(zip(x_all[a:b + 1][::-1], y_of(seabed[a:b + 1][::-1])))
        level = pl.span_level(result, k)
        w.polyline(ring, names[level], closed=True)
        top = int(np.argmin(np.abs(kp - span.kp_max_gap)))
        w.text(x_all[top], y_of(result.cable_m[top]) + th, f"S{k + 1} {span.length_m:.1f} m, gap {span.max_gap_m:.2f} m"
               + (" (indicative)" if level == "indicative" else ""), th * 0.8, names["text"])
    with np.errstate(invalid="ignore"):
        free = np.isfinite(result.gap_m) & (result.gap_m > pl.span_end_tol(result.criteria)) & ~reported
    for a, b in runs(free):
        a, b = max(a - 1, i0), min(b + 1, i1)
        if b - a < 1:
            continue
        ring = list(zip(x_all[a:b + 1], y_of(result.cable_m[a:b + 1]))) + \
            list(zip(x_all[a:b + 1][::-1], y_of(seabed[a:b + 1][::-1])))
        w.polyline(ring, names["clear"], closed=True)

    for sp in result.supports:
        if not (lo <= sp.kp <= hi):
            continue
        k = (sp.i0 + sp.i1) // 2
        x, y = x_all[k], y_of(seabed[k])
        w.polyline([(x - th * 0.4, y - th * 0.6), (x + th * 0.4, y - th * 0.6), (x, y), (x - th * 0.4, y - th * 0.6)],
                   names["supports"])
        radius = sp.bend_radius_m
        if sp.crest or (math.isfinite(sp.reaction_kn) and sp.reaction_kn >= 0.005):
            text = f"P {sp.reaction_kn:.2f} kN" + (f", R {radius:.1f} m" if math.isfinite(radius) else "")
        else:
            text = "touchdown"
        w.text(x, y - th * 1.8, text, th * 0.6, names["supports"])
    for b in result.bodies:
        if not (lo <= b.kp_km <= hi):
            continue
        body = b.body
        i = int(np.clip(np.searchsorted(kp, b.kp_km), 0, st.n - 1))
        x, y = b.kp_km * 1000.0, y_of(result.cable_m[i])
        half = max(body.length_m, th * 0.5) / 2.0
        tall = (body.diameter_m if math.isfinite(body.diameter_m) else 0.3) * ve
        w.polyline([(x - half, y), (x + half, y), (x + half, y + tall), (x - half, y + tall)], names["bodies"],
                   closed=True)
        w.text(x, y + tall + th * 0.5, f"{body.name} ({body.kind})", th * 0.8, names["bodies"])
    index = getattr(st, "protection_index", None)
    for j, item in enumerate(getattr(st, "protection", []) or []):
        if index is None:
            break
        od = item.diameter_m if math.isfinite(item.diameter_m) else 0.0
        for a, b in runs(index == j):
            a, b = max(a, i0), min(b, i1)
            if b <= a:
                continue
            part = np.arange(a, b + 1)
            part = part[_thin(len(part), options.max_vertices)]
            w.polyline(zip(x_all[part], y_of(result.cable_m[part])), names["protection"])
            if od > 0:      # the sleeve's top, its outside diameter above the cable
                w.polyline(zip(x_all[part], y_of(result.cable_m[part] + od)), names["protection"])
            mid = (a + b) // 2
            w.text(x_all[mid], y_of(result.cable_m[mid] + od) + th * 0.5, f"{item.name} ({item.kind})", th * 0.7,
                   names["protection"], align=1)
    for feature in getattr(st, "features", []) or []:
        if not (lo - 0.01 <= feature.kp_km <= hi + 0.01):
            continue
        a, b = feature.extent_m
        x0 = feature.kp_km * 1000.0
        offsets = np.linspace(a, b, 60)
        if feature.kind == "custom":
            offsets = np.unique(np.concatenate((offsets, [p[0] for p in feature.points])))
        base = np.interp(x0 + offsets, x_all, st.obstacle_m - st.feature_m)
        w.polyline(zip(x0 + offsets, y_of(base + feature.height_at(offsets))), names["features"])
        w.text(x0, y_of(np.interp(x0, x_all, seabed)) - th * 3.0, feature.label(), th * 0.7, names["features"],
               align=1)

    # Frame: sea level, KP axis with ticks under the profile, title.
    y_values = np.concatenate([y_of(seabed[i0:i1 + 1]), y_of(result.cable_m[i0:i1 + 1])])
    y_values = y_values[np.isfinite(y_values)]
    y_top, y_bottom = float(y_values.max()), float(y_values.min())
    x0, x1 = float(x_all[i0]), float(x_all[i1])
    w.line(x0, 0.0, x1, 0.0, names["sea"])
    w.text(x0, th * 0.4, "Sea level", th * 0.7, names["sea"])
    axis_y = y_bottom - 3.0 * th
    w.line(x0, axis_y, x1, axis_y, names["frame"])
    step = _nice_step(x1 - x0) / 1000.0
    tick = math.ceil(lo / step) * step
    while tick <= hi + 1e-9:
        tx = tick * 1000.0
        w.line(tx, axis_y, tx, axis_y - th, names["frame"])
        w.text(tx, axis_y - 2.4 * th, f"KP {tick:.3f}", th * 0.8, names["frame"], align=1)
        tick += step
    title = options.title or "Span & Slack Analysis"
    w.text(x0, y_top + 4.0 * th, title, th * 1.4, names["text"])
    control = result.control
    w.text(x0, y_top + 2.2 * th, f"Lay control: {control.mode.replace('_', ' ')} {control.value:g}; "
           f"vertical exaggeration {ve:g}; X = KP x 1000 (m), Y = elevation (m) x {ve:g}", th * 0.8, names["text"])

    if options.graphs:
        panel = max((y_top - y_bottom) * 0.6, length * 0.06, 10.0 * th)
        gap = 6.0 * th
        base = axis_y - 4.0 * th
        def finite(values):   # the pull-taut search limit is not a tension (the cable is short)
            values = np.asarray(values, dtype=float)
            return np.where(values >= 0.999 * pl.TAUT_CAP_KN, np.nan, values)

        if options.slack_basis == "route" or result.used_bottom_pct is None:
            slack = ("SLACK", "Slack over the route (%)", [("LAID", result.laid_pct, 5), ("FILL", result.fill_pct, 34),
                                                           ("USED", result.shape_pct, 8)])
        else:
            slack = ("SLACK", "Bottom slack, over the seabed (%)",
                     [("LAID", result.bottom_slack_pct, 5),
                      ("SEABED", np.where(np.isfinite(result.used_bottom_pct), 0.0, np.nan), 34),
                      ("USED", result.used_bottom_pct, 8)])
        series = [
            ("TENSION", "Tension (kN)", [("RESIDUAL", finite(result.tension_kn), 5),
                                         ("LAY", finite(result.deposit_kn), 8),
                                         ("NPTS", st.npts_kn, 1),
                                         ("MAX_FOR_LIMITS", np.where(result.allowable_kn < 99.9, result.allowable_kn,
                                                                     np.nan) if result.allowable_kn is not None
                                          else np.full(st.n, np.nan), 3)]),
            slack,
            ("SLOPE", "Seabed slope and friction angle (deg)",
             [("SLOPE", np.degrees(np.arctan(np.abs(st.slope))), 34), ("FRICTION_ANGLE", np.degrees(np.arctan(st.mu)),
                                                                        30)]),
        ]
        for key, title_text, curves in series:
            top_y = base
            bottom_y = base - panel
            values = np.concatenate([np.asarray(v, dtype=float)[i0:i1 + 1] for _n, v, _c in curves])
            values = values[np.isfinite(values)]
            if not values.size:
                continue
            vmin, vmax = float(values.min()), float(values.max())
            if vmax - vmin < 1e-9:
                vmin, vmax = vmin - 1.0, vmax + 1.0
            frame = w.layer(f"SA_{key}_FRAME", 7)
            w.polyline([(x0, bottom_y), (x1, bottom_y), (x1, top_y), (x0, top_y)], frame, closed=True)
            w.text(x0, top_y + th * 0.4, title_text, th * 0.9, frame)
            w.text(x0 - th * 0.5, top_y - th, f"{vmax:.2f}", th * 0.7, frame, align=2)
            w.text(x0 - th * 0.5, bottom_y, f"{vmin:.2f}", th * 0.7, frame, align=2)
            for name, v, colour in curves:
                arr = np.asarray(v, dtype=float)
                layer = w.layer(f"SA_{key}_{name}", colour)
                yy = bottom_y + (arr[keep] - vmin) / (vmax - vmin) * panel
                good = np.isfinite(yy)
                for a, b in runs(good):
                    w.polyline(zip(x_all[keep][a:b + 1], yy[a:b + 1]), layer)
            base = bottom_y - gap
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(w.document())
    return dict(w.layers)
