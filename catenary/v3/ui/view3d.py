# -*- coding: utf-8 -*-
"""Software-projected interactive 3D viewport for the Cable Lay Simulator.

Renders a :class:`~catenary.v3.ui.scene.SceneData` with QPainter only (no
OpenGL, no pyqtgraph). A turntable perspective camera projects every scene
vertex in a single NumPy matrix multiply per frame; the seabed is drawn with
a painter's-algorithm depth sort, then water plane, cables, markers, vessel
and screen-space HUD overlays on top.

Public API: :class:`View3DWidget` with ``set_scene``, ``set_z_exaggeration``
(and the ``z_exaggeration`` property), ``set_cable_color_mode``,
``fit_view`` and the ``hoverInfo`` / ``pointPicked`` signals. Everything
else is private.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from qgis.PyQt import QtCore, QtGui
    from qgis.PyQt.QtCore import Qt, pyqtSignal
    from qgis.PyQt.QtWidgets import QToolButton, QWidget
except Exception:  # pragma: no cover - standalone (non-QGIS) use
    from PyQt5 import QtCore, QtGui
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtWidgets import QToolButton, QWidget

from .scene import chute_arc_points, vessel_chute_xyz, vessel_crp_xy, vessel_footprint

# Qt5/Qt6 enum holders (Qt6 scopes enums; Qt5 exposes them on Qt itself).
_MOUSE_BUTTON = getattr(Qt, "MouseButton", Qt)
_PEN_STYLE = getattr(Qt, "PenStyle", Qt)
_BRUSH_STYLE = getattr(Qt, "BrushStyle", Qt)
_PEN_CAP = getattr(Qt, "PenCapStyle", Qt)
_PEN_JOIN = getattr(Qt, "PenJoinStyle", Qt)
_FOCUS_POLICY = getattr(Qt, "FocusPolicy", Qt)
_RENDER_HINT = getattr(QtGui.QPainter, "RenderHint", QtGui.QPainter)

_FOV_DEG = 45.0
_MAX_BED_VERTS = 81            # per axis -> at most ~80x80 quads
_PICK_RADIUS_PX = 12.0
_DRAG_THRESHOLD_PX = 4.0
_DEFAULT_YAW_DEG = 55.0
_DEFAULT_PITCH_DEG = -30.0
_TENSION_BINS = 48

# Small viridis-like ramp (RGB stops, dark purple -> yellow).
_VIRIDIS_STOPS = np.array([
    (68, 1, 84), (72, 40, 120), (62, 74, 137), (49, 104, 142),
    (38, 130, 142), (31, 158, 137), (53, 183, 121), (109, 205, 89),
    (180, 222, 44), (253, 231, 37),
], dtype=float)

# Seabed elevation ramp: deepest (dark navy) -> shallowest (sandy brown).
_BED_STOPS = np.array([
    (14, 22, 42), (22, 46, 76), (32, 76, 98), (52, 102, 110),
    (96, 118, 100), (150, 128, 90),
], dtype=float)

_HUD_TEXT = QtGui.QColor(228, 235, 242)
_HUD_HALO = QtGui.QColor(10, 16, 26, 210)


def _ramp(stops: "np.ndarray", t: "np.ndarray") -> "np.ndarray":
    """Piecewise-linear color ramp; ``t`` in [0, 1] -> (n, 3) floats."""
    t = np.clip(np.nan_to_num(np.asarray(t, dtype=float)), 0.0, 1.0)
    pos = t * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    f = (pos - i)[..., None]
    return stops[i] * (1.0 - f) + stops[i + 1] * f


def _event_xy(event: Any) -> Tuple[float, float]:
    """Cursor position from a mouse/wheel event, Qt5/Qt6 tolerant."""
    getter = getattr(event, "position", None)
    if getter is not None:
        try:
            p = getter()
            return float(p.x()), float(p.y())
        except TypeError:
            pass
    p = event.pos()
    return float(p.x()), float(p.y())


def _true_runs(mask: "np.ndarray") -> List[Tuple[int, int]]:
    """Contiguous True runs of ``mask`` as (start, stop) index pairs."""
    if mask.size == 0 or not mask.any():
        return []
    m = mask.astype(np.int8)
    d = np.diff(m)
    starts = (np.flatnonzero(d == 1) + 1).tolist()
    stops = (np.flatnonzero(d == -1) + 1).tolist()
    if m[0]:
        starts.insert(0, 0)
    if m[-1]:
        stops.append(mask.size)
    return list(zip(starts, stops))


def _nice_number(v: float) -> float:
    """Round ``v`` up to a 1/2/5 x 10^k 'nice' value."""
    if not math.isfinite(v) or v <= 0:
        return 1.0
    exp = math.floor(math.log10(v))
    base = v / (10.0 ** exp)
    for n in (1.0, 2.0, 5.0, 10.0):
        if base <= n + 1e-9:
            return n * (10.0 ** exp)
    return 10.0 ** (exp + 1)


class _SceneCache:
    """Per-scene geometry flattened into one shared vertex array."""

    def __init__(self) -> None:
        self.pts = np.zeros((0, 3), dtype=float)      # world coords, unexaggerated
        # Seabed grid (None when absent or degenerate).
        self.bed_quads: Optional[Tuple["np.ndarray", ...]] = None  # i00,i10,i11,i01 (global)
        self.bed_quad_t: Optional["np.ndarray"] = None             # elevation ramp position
        self.bed_slice: Optional[slice] = None
        self.bed_profile_slice: Optional[slice] = None
        # Water plane: 4 corners then grid-line endpoint pairs.
        self.water_slice: Optional[slice] = None
        self.water_n_gridlines = 0
        # Cables: aligned with the (sanitized) scene cable list.
        self.cable_slices: List[slice] = []
        self.cable_data: List[Dict[str, Any]] = []
        self.tension_range: Optional[Tuple[float, float]] = None
        # Markers / vessel.
        self.marker_slice: Optional[slice] = None
        self.marker_info: List[Tuple[str, str, str, float]] = []   # kind, label, color, size
        self.vessel_slice: Optional[slice] = None        # waterline footprint
        self.vessel_deck_slice: Optional[slice] = None   # deck footprint (extruded hull)
        self.vessel_chute_slice: Optional[slice] = None  # chute point + CRP point
        self.chute_arc_slice: Optional[slice] = None     # overboarding chute arc
        self.vessel_sheaves_slice: Optional[slice] = None  # individual sheave points
        self.vessel_sheave_labels: List[str] = []
        self.trail_slice: Optional[slice] = None          # vessel snail trail
        self.departure_label: str = "chute"              # text at the departure anchor
        self.vessel_color = "#444444"
        self.vessel_label = ""


class View3DWidget(QWidget):
    """Interactive software-rendered 3D view of a cable lay scene."""

    hoverInfo = pyqtSignal(str)
    pointPicked = pyqtSignal(int, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(_FOCUS_POLICY.StrongFocus)
        self._scene: Any = None
        self._cache: Optional[_SceneCache] = None
        self._zex = 1.0
        self._color_mode = "segment"
        self._has_view = False
        # Camera (target kept in unexaggerated world coordinates).
        self._yaw = _DEFAULT_YAW_DEG
        self._pitch = _DEFAULT_PITCH_DEG
        self._distance = 500.0
        self._target = np.zeros(3, dtype=float)
        # Interaction state.
        self._drag_button: Optional[Any] = None
        self._drag_last: Tuple[float, float] = (0.0, 0.0)
        self._drag_total = 0.0
        self._press_pos: Optional[Tuple[float, float]] = None
        self._mouse_pos: Optional[Tuple[float, float]] = None
        self._hover: Optional[Tuple[int, int]] = None
        self._hover_text = ""
        self._orbit_pivot: Optional["np.ndarray"] = None   # exaggerated coords
        self._build_nav_buttons()
        # Last-frame projection (for hover/pick) and derived caches.
        self._proj: Optional[Tuple["np.ndarray", ...]] = None
        self._bed_brush_cache: Optional[Tuple[float, List[QtGui.QBrush]]] = None
        self._run_cache: Dict[Tuple[int, str], List[Tuple[int, int, QtGui.QPen]]] = {}

    # ------------------------------------------------------------------ API

    def set_scene(self, scene: Any, preserve_view: bool = True) -> None:
        """Replace the displayed scene (``None`` clears the view)."""
        self._scene = scene
        self._cache = self._build_cache(scene) if scene is not None else None
        self._bed_brush_cache = None
        self._run_cache.clear()
        self._hover = None
        self._hover_text = ""
        self._proj = None
        if scene is not None and (not self._has_view or not preserve_view):
            self.fit_view()
        self.update()

    @property
    def z_exaggeration(self) -> float:
        return self._zex

    @z_exaggeration.setter
    def z_exaggeration(self, value: float) -> None:
        self.set_z_exaggeration(value)

    def set_z_exaggeration(self, value: float) -> None:
        """Set the vertical exaggeration factor applied before projection."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(v) or v <= 0:
            return
        v = min(max(v, 0.01), 1000.0)
        if v != self._zex:
            self._zex = v
            self._bed_brush_cache = None
            self.update()

    def set_cable_color_mode(self, mode: str) -> None:
        """Select cable coloring: ``'segment'`` or ``'tension'``."""
        mode = str(mode).lower()
        if mode not in ("segment", "tension"):
            mode = "segment"
        if mode != self._color_mode:
            self._color_mode = mode
            self._run_cache.clear()
            self.update()

    def fit_view(self) -> None:
        """Frame the current scene bounds (keeps orbit orientation)."""
        if self._scene is not None:
            (x0, x1), (y0, y1), (z0, z1) = self._scene.compute_bounds()
        else:
            (x0, x1), (y0, y1), (z0, z1) = (-100.0, 100.0), (-100.0, 100.0), (-100.0, 0.0)
        self._target = np.array([(x0 + x1) * 0.5, (y0 + y1) * 0.5, (z0 + z1) * 0.5])
        dz = (z1 - z0) * self._zex
        radius = 0.5 * math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + dz ** 2)
        radius = max(radius, 1.0)
        self._distance = radius / math.sin(math.radians(_FOV_DEG) * 0.5) * 1.15
        self._has_view = True
        self.update()

    # ------------------------------------------------------- view presets

    def _build_nav_buttons(self) -> None:
        self._nav_buttons: List[QToolButton] = []
        for text, tip, cb in (
            ("Fit", "Zoom to the extents of the model", self.fit_view),
            ("Ship", "Zoom to the vessel", self.zoom_to_ship),
            ("Plan", "Plan view (top down, north up)", self.view_plan),
            ("Side", "Elevation view (across the ship heading)", self.view_side),
            ("Bow", "Look aft from ahead of the ship", self.view_bow),
            ("Stern", "Look forward from astern", self.view_stern),
        ):
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setCursor(getattr(getattr(Qt, "CursorShape", Qt), "PointingHandCursor"))
            b.setStyleSheet(
                "QToolButton { background: rgba(18, 28, 40, 170); color: #dfe6ee;"
                " border: 1px solid rgba(255, 255, 255, 55); border-radius: 3px;"
                " padding: 2px 6px; font-size: 11px; }"
                "QToolButton:hover { background: rgba(50, 70, 95, 200); }"
            )
            b.clicked.connect(cb)
            b.show()
            self._nav_buttons.append(b)
        self._layout_nav_buttons()

    def _layout_nav_buttons(self) -> None:
        w = max((b.sizeHint().width() for b in self._nav_buttons), default=0)
        y = 8
        for b in self._nav_buttons:
            b.resize(w, b.sizeHint().height())
            b.move(self.width() - w - 8, y)
            y += b.height() + 4

    def _vessel_heading_deg(self) -> float:
        v = getattr(self._scene, "vessel", None)
        return float(getattr(v, "heading_deg", 0.0)) if v is not None else 0.0

    def _set_orientation(self, yaw_deg: float, pitch_deg: float) -> None:
        self._yaw = float(yaw_deg) % 360.0
        self._pitch = min(max(float(pitch_deg), -89.0), 89.0)
        self.update()

    def view_plan(self) -> None:
        """Top-down; +y (north) up the screen."""
        self._set_orientation(90.0, -89.0)

    def view_side(self) -> None:
        """True elevation, looking across the ship heading (profile)."""
        self._set_orientation(self._vessel_heading_deg() + 90.0, 0.0)

    def view_bow(self) -> None:
        self._set_orientation(self._vessel_heading_deg() + 180.0, -10.0)

    def view_stern(self) -> None:
        self._set_orientation(self._vessel_heading_deg(), -10.0)

    def zoom_to_ship(self) -> None:
        """Frame the vessel (keeps the current orbit orientation)."""
        v = getattr(self._scene, "vessel", None)
        if v is None:
            return
        foot = vessel_footprint(v)
        cx, cy = float(foot[:, 0].mean()), float(foot[:, 1].mean())
        wz = float(getattr(self._scene, "water_z", 0.0))
        h = float(getattr(v, "height_m", 0.0)) or 4.0
        r = float(np.max(np.hypot(foot[:, 0] - cx, foot[:, 1] - cy)))
        r = max(r, h * self._zex, 1.0) * 1.25
        self._target = np.array([cx, cy, wz + 0.5 * h])
        self._distance = r / math.sin(math.radians(_FOV_DEG) * 0.5) * 1.15
        self._has_view = True
        self.update()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(640, 480)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(200, 150)

    # --------------------------------------------------------- scene cache

    def _build_cache(self, scene: Any) -> _SceneCache:
        cache = _SceneCache()
        chunks: List["np.ndarray"] = []
        cursor = 0

        def push(arr: "np.ndarray") -> slice:
            nonlocal cursor
            arr = np.asarray(arr, dtype=float).reshape(-1, 3)
            chunks.append(arr)
            sl = slice(cursor, cursor + len(arr))
            cursor += len(arr)
            return sl

        bed = getattr(scene, "bed", None)
        if bed is not None:
            self._cache_bed(bed, cache, push)

        (bx0, bx1), (by0, by1), _ = scene.compute_bounds()
        if getattr(scene, "show_water_plane", True) and bx1 > bx0 and by1 > by0:
            self._cache_water(scene, (bx0, bx1, by0, by1), cache, push)

        tmins, tmaxs = [], []
        for path in getattr(scene, "cables", []) or []:
            data = self._sanitize_cable(path)
            cache.cable_data.append(data)
            cache.cable_slices.append(push(data["xyz"]))
            t = data.get("tension")
            if t is not None and t.size:
                tmins.append(float(t.min()))
                tmaxs.append(float(t.max()))
        if tmins:
            cache.tension_range = (min(tmins), max(tmaxs))

        mk_pts = []
        for m in getattr(scene, "markers", []) or []:
            p = np.asarray(m.xyz, dtype=float).ravel()[:3]
            if p.size == 3 and np.isfinite(p).all():
                mk_pts.append(p)
                cache.marker_info.append(
                    (str(getattr(m, "kind", "point")), str(getattr(m, "label", "")),
                     str(getattr(m, "color", "#d62728")), float(getattr(m, "size", 6.0)))
                )
        if mk_pts:
            cache.marker_slice = push(np.array(mk_pts))

        trail = getattr(scene, "vessel_trail", None)
        if trail is not None:
            tr = np.asarray(trail, dtype=float).reshape(-1, 2)
            tr = tr[np.isfinite(tr).all(axis=1)]
            if len(tr) >= 2:
                wz = float(getattr(scene, "water_z", 0.0))
                cache.trail_slice = push(
                    np.column_stack([tr, np.full(len(tr), wz)]))

        vessel = getattr(scene, "vessel", None)
        if vessel is not None and np.isfinite(np.asarray(vessel.xy, dtype=float)).all():
            wz = float(getattr(scene, "water_z", 0.0))
            foot = vessel_footprint(vessel)
            base = np.column_stack([foot, np.full(len(foot), wz)])
            cache.vessel_slice = push(base)
            height = float(getattr(vessel, "height_m", 0.0))
            if height > 0.0:
                deck = base.copy()
                deck[:, 2] = wz + height
                cache.vessel_deck_slice = push(deck)
                crp = vessel_crp_xy(vessel)
                cache.vessel_chute_slice = push(np.array([
                    vessel_chute_xyz(vessel, wz),
                    (crp[0], crp[1], wz + height),
                ]))
                arc = chute_arc_points(vessel, wz)
                if arc is not None:
                    cache.chute_arc_slice = push(arc)
            sheaves = getattr(vessel, "sheaves_xy", None) or []
            if sheaves:
                cache.vessel_sheave_labels = [str(s[0]) for s in sheaves]
                cache.vessel_sheaves_slice = push(np.array(
                    [(float(s[1]), float(s[2]), wz + height) for s in sheaves]))
            cache.vessel_color = str(getattr(vessel, "color", "#444444"))
            cache.vessel_label = str(getattr(vessel, "label", ""))
            cache.departure_label = str(getattr(vessel, "departure_label", "chute"))

        cache.pts = np.vstack(chunks) if chunks else np.zeros((0, 3), dtype=float)
        return cache

    def _cache_bed(self, bed: Any, cache: _SceneCache, push: Any) -> None:
        try:
            x = np.asarray(bed.x, dtype=float).ravel()
            y = np.asarray(bed.y, dtype=float).ravel()
            z = np.asarray(bed.z, dtype=float)
        except Exception:
            return
        if z.ndim == 1:
            z = z.reshape(1, -1)
        if x.size < 1 or y.size < 1 or z.shape != (y.size, x.size):
            return
        if min(x.size, y.size) < 2:
            # Degenerate grid: draw as a profile polyline along the long axis.
            if x.size >= 2:
                prof = np.column_stack([x, np.full(x.size, y[0] if y.size else 0.0), z.ravel()[: x.size]])
            elif y.size >= 2:
                prof = np.column_stack([np.full(y.size, x[0]), y, z.ravel()[: y.size]])
            else:
                return
            finite = np.isfinite(prof).all(axis=1)
            if finite.sum() >= 2:
                cache.bed_profile_slice = push(prof[finite])
            return

        ix = self._decimate_indices(x.size)
        iy = self._decimate_indices(y.size)
        xd, yd = x[ix], y[iy]
        zd = z[np.ix_(iy, ix)]
        nxd, nyd = xd.size, yd.size
        gx, gy = np.meshgrid(xd, yd)
        verts = np.column_stack([gx.ravel(), gy.ravel(), np.nan_to_num(zd.ravel(), nan=0.0)])
        vert_ok = np.isfinite(zd.ravel())
        sl = push(verts)
        cache.bed_slice = sl

        ids = np.arange(nyd * nxd).reshape(nyd, nxd)
        i00 = ids[:-1, :-1].ravel()
        i10 = ids[:-1, 1:].ravel()
        i11 = ids[1:, 1:].ravel()
        i01 = ids[1:, :-1].ravel()
        ok = vert_ok[i00] & vert_ok[i10] & vert_ok[i11] & vert_ok[i01]
        i00, i10, i11, i01 = i00[ok], i10[ok], i11[ok], i01[ok]
        if i00.size == 0:
            cache.bed_slice = None
            return
        zq = (verts[i00, 2] + verts[i10, 2] + verts[i11, 2] + verts[i01, 2]) * 0.25
        zmin, zmax = float(np.nanmin(zd)), float(np.nanmax(zd))
        span = zmax - zmin
        flat_eps = max(1.0, 0.01 * max(abs(zmin), abs(zmax)))
        if span < flat_eps:
            # A (near-)flat bed would collapse to the darkest ramp stop and
            # render almost black; use a fixed mid-tone instead.
            cache.bed_quad_t = np.full(i00.shape, 0.55)
        else:
            cache.bed_quad_t = (zq - zmin) / span
        off = sl.start
        cache.bed_quads = (i00 + off, i10 + off, i11 + off, i01 + off)

    @staticmethod
    def _decimate_indices(n: int, max_n: int = _MAX_BED_VERTS) -> "np.ndarray":
        if n <= max_n:
            return np.arange(n)
        return np.unique(np.round(np.linspace(0, n - 1, max_n)).astype(int))

    def _cache_water(self, scene: Any, xybounds: Tuple[float, float, float, float],
                     cache: _SceneCache, push: Any) -> None:
        x0, x1, y0, y1 = xybounds
        wz = float(getattr(scene, "water_z", 0.0))
        pts = [(x0, y0, wz), (x1, y0, wz), (x1, y1, wz), (x0, y1, wz)]
        step = _nice_number(max(x1 - x0, y1 - y0) / 8.0)
        lines = 0
        for gx in np.arange(math.ceil(x0 / step) * step, x1, step):
            if lines >= 25:
                break
            pts += [(gx, y0, wz), (gx, y1, wz)]
            lines += 1
        n_x_lines = lines
        for gy in np.arange(math.ceil(y0 / step) * step, y1, step):
            if lines - n_x_lines >= 25:
                break
            pts += [(x0, gy, wz), (x1, gy, wz)]
            lines += 1
        cache.water_slice = push(np.array(pts, dtype=float))
        cache.water_n_gridlines = lines

    @staticmethod
    def _sanitize_cable(path: Any) -> Dict[str, Any]:
        xyz = np.asarray(path.xyz, dtype=float)
        if xyz.ndim != 2 or xyz.shape[1] < 3:
            xyz = xyz.reshape(-1, 3) if xyz.size % 3 == 0 else np.zeros((0, 3))
        xyz = xyz[:, :3]
        n = len(xyz)
        finite = np.isfinite(xyz).all(axis=1)

        def aligned(name: str) -> Optional["np.ndarray"]:
            arr = getattr(path, name, None)
            if arr is None:
                return None
            arr = np.asarray(arr).ravel()
            if arr.size != n:
                return None
            return arr[finite]

        return {
            "xyz": xyz[finite],
            "name": str(getattr(path, "name", "cable")),
            "tension": aligned("tension_kN"),
            "s": aligned("s_m"),
            "segment_index": aligned("segment_index"),
            "segment_colors": list(getattr(path, "segment_colors", None) or []),
            "contact": aligned("contact"),
            "color": str(getattr(path, "color", "#1f77b4")),
            "width": max(float(getattr(path, "width", 2.0) or 2.0), 0.5),
        }

    # ------------------------------------------------------------- camera

    @staticmethod
    def _rotation_rows(yaw_deg: float, pitch_deg: float) -> "np.ndarray":
        """World->camera rotation rows (right, up, fwd) for a turntable."""
        p = math.radians(pitch_deg)
        yw = math.radians(yaw_deg)
        fwd = np.array([math.cos(p) * math.cos(yw), math.cos(p) * math.sin(yw), math.sin(p)])
        right = np.cross(fwd, np.array([0.0, 0.0, 1.0]))
        norm = float(np.linalg.norm(right))
        right = right / norm if norm > 1e-9 else np.array([1.0, 0.0, 0.0])
        up = np.cross(right, fwd)
        return np.stack([right, up, fwd])

    def _camera(self) -> Tuple["np.ndarray", "np.ndarray"]:
        """Eye position (exaggerated space) and rotation rows (right, up, fwd)."""
        rot = self._rotation_rows(self._yaw, self._pitch)
        tgt = np.array([self._target[0], self._target[1], self._target[2] * self._zex])
        eye = tgt - rot[2] * self._distance
        return eye, rot

    def _pivot_under_cursor(self, mx: float, my: float) -> "np.ndarray":
        """Orbit pivot in exaggerated space: the cable point under the
        cursor when there is one, else the view ray's intersection with the
        horizontal plane through the current target, else the target. This
        keeps the point you grab pinned under the cursor while orbiting."""
        eye, rot = self._camera()
        tgt = np.array([self._target[0], self._target[1], self._target[2] * self._zex])
        cache = self._cache
        hit = self._pick(mx, my)
        if hit is not None and cache is not None:
            gi = cache.cable_slices[hit[0]].start + hit[1]
            return cache.pts[gi] * np.array([1.0, 1.0, self._zex])
        f = self._focal_px()
        d_cam = np.array([(mx - self.width() * 0.5) / f,
                          -(my - self.height() * 0.5) / f, 1.0])
        d = rot.T @ d_cam
        nrm = float(np.linalg.norm(d))
        if nrm > 1e-9 and abs(d[2] / nrm) > 1e-6:
            d = d / nrm
            t = (tgt[2] - eye[2]) / d[2]
            if 0.05 * self._distance < t < 20.0 * self._distance:
                return eye + t * d
        return tgt

    def _orbit(self, dyaw_deg: float, dpitch_deg: float) -> None:
        """Rotate the camera about the grabbed pivot, keeping the pivot
        fixed in screen space (no fly-away when the target is off-model)."""
        pivot = self._orbit_pivot
        eye_old, rot_old = self._camera()
        self._yaw = (self._yaw + dyaw_deg) % 360.0
        self._pitch = min(max(self._pitch + dpitch_deg, -89.0), 89.0)
        if pivot is None:
            return
        rot_new = self._rotation_rows(self._yaw, self._pitch)
        cam_space = rot_old @ (pivot - eye_old)
        eye_new = pivot - rot_new.T @ cam_space
        tgt = eye_new + rot_new[2] * self._distance
        self._target = np.array([tgt[0], tgt[1], tgt[2] / self._zex])

    def _focal_px(self) -> float:
        return (max(self.height(), 1) * 0.5) / math.tan(math.radians(_FOV_DEG) * 0.5)

    def _metres_per_px(self) -> float:
        return 2.0 * self._distance * math.tan(math.radians(_FOV_DEG) * 0.5) / max(self.height(), 1)

    def _project_all(self) -> Optional[Tuple["np.ndarray", ...]]:
        """Project the whole cached vertex array; returns (px, py, depth, valid)."""
        if self._cache is None or self._cache.pts.shape[0] == 0:
            return None
        pts = self._cache.pts * np.array([1.0, 1.0, self._zex])
        eye, rot = self._camera()
        cam = (pts - eye) @ rot.T
        cz = cam[:, 2]
        near = max(1e-2, self._distance * 1e-3)
        valid = cz > near
        safe = np.where(valid, cz, 1.0)
        f = self._focal_px()
        px = self.width() * 0.5 + cam[:, 0] * f / safe
        py = self.height() * 0.5 - cam[:, 1] * f / safe
        return px, py, cz, valid

    # ------------------------------------------------------------ painting

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt naming)
        painter = QtGui.QPainter(self)
        try:
            self._draw_background(painter)
            if self._scene is None or self._cache is None:
                self._draw_watermark(painter, "No solution")
                return
            self._proj = self._project_all()
            if self._proj is not None:
                eye, _ = self._camera()
                above_water = eye[2] > float(getattr(self._scene, "water_z", 0.0)) * self._zex
                if not above_water:
                    self._draw_water(painter)
                self._draw_bed(painter)
                if above_water:
                    self._draw_water(painter)
                painter.setRenderHint(_RENDER_HINT.Antialiasing, True)
                self._draw_cables(painter)
                self._draw_markers(painter)
                self._draw_vessel(painter)
                self._draw_hover(painter)
            self._draw_hud(painter)
        finally:
            painter.end()

    def _draw_background(self, painter: QtGui.QPainter) -> None:
        grad = QtGui.QLinearGradient(0, 0, 0, max(self.height(), 1))
        grad.setColorAt(0.0, QtGui.QColor(24, 32, 44))
        grad.setColorAt(1.0, QtGui.QColor(48, 62, 80))
        painter.fillRect(self.rect(), QtGui.QBrush(grad))

    def _draw_watermark(self, painter: QtGui.QPainter, text: str) -> None:
        painter.setRenderHint(_RENDER_HINT.Antialiasing, True)
        font = painter.font()
        font.setPointSizeF(16.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(200, 210, 220, 90))
        fm = QtGui.QFontMetrics(font)
        w = fm.horizontalAdvance(text)
        painter.drawText(QtCore.QPointF((self.width() - w) * 0.5, self.height() * 0.5), text)

    def _bed_brushes(self) -> List[QtGui.QBrush]:
        cached = self._bed_brush_cache
        if cached is not None and cached[0] == self._zex:
            return cached[1]
        cache = self._cache
        assert cache is not None and cache.bed_quads is not None
        i00, i10, i11, i01 = cache.bed_quads
        pts = cache.pts * np.array([1.0, 1.0, self._zex])
        e1 = pts[i10] - pts[i00]
        e2 = pts[i01] - pts[i00]
        nrm = np.cross(e1, e2)
        ln = np.linalg.norm(nrm, axis=1)
        ln[ln < 1e-12] = 1.0
        nrm /= ln[:, None]
        light = np.array([0.35, 0.25, 0.9])
        light /= np.linalg.norm(light)
        shade = 0.55 + 0.45 * np.abs(nrm @ light)
        rgb = np.clip(_ramp(_BED_STOPS, cache.bed_quad_t) * shade[:, None], 0, 255).astype(int)
        brushes = [QtGui.QBrush(QtGui.QColor(int(r), int(g), int(b))) for r, g, b in rgb]
        self._bed_brush_cache = (self._zex, brushes)
        return brushes

    def _draw_bed(self, painter: QtGui.QPainter) -> None:
        cache = self._cache
        px, py, cz, valid = self._proj  # type: ignore[misc]
        if cache.bed_profile_slice is not None:
            painter.setRenderHint(_RENDER_HINT.Antialiasing, True)
            pen = QtGui.QPen(QtGui.QColor(120, 96, 66), 2.0)
            painter.setPen(pen)
            sl = cache.bed_profile_slice
            self._draw_masked_polyline(painter, px[sl], py[sl], valid[sl])
            return
        if cache.bed_quads is None:
            return
        painter.setRenderHint(_RENDER_HINT.Antialiasing, False)
        i00, i10, i11, i01 = cache.bed_quads
        qvalid = valid[i00] & valid[i10] & valid[i11] & valid[i01]
        depth = (cz[i00] + cz[i10] + cz[i11] + cz[i01]) * 0.25
        order = np.argsort(-depth)
        order = order[qvalid[order]]
        brushes = self._bed_brushes()
        painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 38), 0))
        x0, y0 = px[i00].tolist(), py[i00].tolist()
        x1, y1 = px[i10].tolist(), py[i10].tolist()
        x2, y2 = px[i11].tolist(), py[i11].tolist()
        x3, y3 = px[i01].tolist(), py[i01].tolist()
        point = QtCore.QPointF
        polygon = QtGui.QPolygonF
        for k in order.tolist():
            painter.setBrush(brushes[k])
            painter.drawPolygon(polygon([
                point(x0[k], y0[k]), point(x1[k], y1[k]),
                point(x2[k], y2[k]), point(x3[k], y3[k]),
            ]))

    def _draw_water(self, painter: QtGui.QPainter) -> None:
        cache = self._cache
        if cache.water_slice is None:
            return
        px, py, _, valid = self._proj  # type: ignore[misc]
        sl = cache.water_slice
        painter.setRenderHint(_RENDER_HINT.Antialiasing, True)
        if valid[sl][:4].all():
            corners = QtGui.QPolygonF([
                QtCore.QPointF(px[sl.start + i], py[sl.start + i]) for i in range(4)
            ])
            painter.setPen(QtGui.QPen(QtGui.QColor(150, 205, 240, 90), 1.0))
            painter.setBrush(QtGui.QBrush(QtGui.QColor(110, 175, 220, 38)))
            painter.drawPolygon(corners)
        painter.setPen(QtGui.QPen(QtGui.QColor(160, 210, 240, 55), 1.0))
        base = sl.start + 4
        for i in range(cache.water_n_gridlines):
            a, b = base + 2 * i, base + 2 * i + 1
            if valid[a] and valid[b]:
                painter.drawLine(QtCore.QPointF(px[a], py[a]), QtCore.QPointF(px[b], py[b]))

    def _cable_runs(self, ci: int) -> List[Tuple[int, int, QtGui.QPen]]:
        """Vertex-range color runs [(start, stop_exclusive, pen), ...] for cable ci."""
        key = (ci, self._color_mode)
        if key in self._run_cache:
            return self._run_cache[key]
        cache = self._cache
        assert cache is not None
        data = cache.cable_data[ci]
        n = len(data["xyz"])
        width = data["width"]

        def pen(color: QtGui.QColor) -> QtGui.QPen:
            p = QtGui.QPen(color, width)
            p.setCapStyle(_PEN_CAP.RoundCap)
            p.setJoinStyle(_PEN_JOIN.RoundJoin)
            return p

        runs: List[Tuple[int, int, QtGui.QPen]] = []
        bins: Optional["np.ndarray"] = None
        colors: Optional[List[QtGui.QColor]] = None
        if self._color_mode == "tension" and data["tension"] is not None and n:
            t = np.asarray(data["tension"], dtype=float)
            rng = cache.tension_range or (float(np.nanmin(t)), float(np.nanmax(t)))
            span = rng[1] - rng[0] if rng[1] > rng[0] else 1.0
            frac = np.nan_to_num((t - rng[0]) / span)
            bins = np.clip((frac * (_TENSION_BINS - 1)).astype(int), 0, _TENSION_BINS - 1)
            lut = _ramp(_VIRIDIS_STOPS, np.linspace(0.0, 1.0, _TENSION_BINS)).astype(int)
            colors = [QtGui.QColor(int(r), int(g), int(b)) for r, g, b in lut]
        elif self._color_mode == "segment" and data["segment_index"] is not None and n:
            seg = np.nan_to_num(np.asarray(data["segment_index"], dtype=float)).astype(int)
            seg_colors = data["segment_colors"]
            if seg_colors:
                bins = seg % len(seg_colors)
                colors = [QtGui.QColor(c) for c in seg_colors]
            else:
                bins = None
        if bins is None or colors is None:
            runs.append((0, n, pen(QtGui.QColor(data["color"]))))
        else:
            change = np.flatnonzero(np.diff(bins))
            starts = np.r_[0, change + 1]
            stops = np.r_[change + 1, n]
            for a, b in zip(starts.tolist(), stops.tolist()):
                # Extend by one vertex so adjacent runs connect visually.
                runs.append((a, min(b + 1, n), pen(colors[int(bins[a])])))
        self._run_cache[key] = runs
        return runs

    @staticmethod
    def _draw_masked_polyline(painter: QtGui.QPainter, px: "np.ndarray",
                              py: "np.ndarray", valid: "np.ndarray") -> None:
        point = QtCore.QPointF
        for a, b in _true_runs(valid):
            if b - a >= 2:
                painter.drawPolyline(QtGui.QPolygonF(
                    [point(x, y) for x, y in zip(px[a:b].tolist(), py[a:b].tolist())]
                ))
            elif b - a == 1:
                painter.drawPoint(point(float(px[a]), float(py[a])))

    def _draw_cables(self, painter: QtGui.QPainter) -> None:
        cache = self._cache
        px, py, _, valid = self._proj  # type: ignore[misc]
        for ci, sl in enumerate(cache.cable_slices):
            data = cache.cable_data[ci]
            cpx, cpy, cvalid = px[sl], py[sl], valid[sl]
            if len(cpx) == 0:
                continue
            for a, b, pen in self._cable_runs(ci):
                painter.setPen(pen)
                self._draw_masked_polyline(painter, cpx[a:b], cpy[a:b], cvalid[a:b])
            contact = data["contact"]
            if contact is not None:
                on = np.flatnonzero(np.asarray(contact, dtype=bool) & cvalid)
                if on.size:
                    # Translucent dark dots subtly darken the run color; thin
                    # them by screen distance so overlapping alphas don't stack.
                    if on.size > 1:
                        gap = (data["width"] + 1.0) * 2.5
                        cum = np.r_[0.0, np.cumsum(np.hypot(
                            np.diff(cpx[on]), np.diff(cpy[on])))]
                        keep = np.unique((cum // gap).astype(int), return_index=True)[1]
                        on = on[keep]
                    dot_pen = QtGui.QPen(QtGui.QColor(10, 10, 15, 70),
                                         data["width"] + 1.0)
                    dot_pen.setCapStyle(_PEN_CAP.RoundCap)
                    painter.setPen(dot_pen)
                    pts = QtGui.QPolygonF([
                        QtCore.QPointF(x, y)
                        for x, y in zip(cpx[on].tolist(), cpy[on].tolist())
                    ])
                    painter.drawPoints(pts)

    def _draw_markers(self, painter: QtGui.QPainter) -> None:
        cache = self._cache
        if cache.marker_slice is None:
            return
        px, py, _, valid = self._proj  # type: ignore[misc]
        sl = cache.marker_slice
        for i, (kind, label, color, size) in enumerate(cache.marker_info):
            gi = sl.start + i
            if not valid[gi]:
                continue
            x, y = float(px[gi]), float(py[gi])
            r = max(size * 0.5, 2.5)
            col = QtGui.QColor(color)
            painter.setPen(QtGui.QPen(QtGui.QColor(240, 244, 248, 220), 1.2))
            painter.setBrush(QtGui.QBrush(col))
            point = QtCore.QPointF
            if kind == "tdp":
                painter.drawPolygon(QtGui.QPolygonF(
                    [point(x, y - r), point(x - r, y + r), point(x + r, y + r)]))
            elif kind == "anchor":
                painter.drawRect(QtCore.QRectF(x - r, y - r, 2 * r, 2 * r))
            elif kind == "junction":
                painter.drawPolygon(QtGui.QPolygonF(
                    [point(x, y - r), point(x + r, y), point(x, y + r), point(x - r, y)]))
            elif kind == "joint":
                # Splice symbol: a circle with a bar across it.
                painter.drawEllipse(QtCore.QRectF(x - r, y - r, 2 * r, 2 * r))
                painter.setPen(QtGui.QPen(QtGui.QColor(240, 244, 248, 230), 1.6))
                painter.drawLine(point(x - r * 1.5, y), point(x + r * 1.5, y))
            elif kind == "target":
                # Crosshair: open circle with tick marks.
                painter.setBrush(QtGui.QBrush())
                painter.setPen(QtGui.QPen(col, 1.8))
                painter.drawEllipse(QtCore.QRectF(x - r, y - r, 2 * r, 2 * r))
                for ddx, ddy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    painter.drawLine(point(x + ddx * r * 0.5, y + ddy * r * 0.5),
                                     point(x + ddx * r * 1.6, y + ddy * r * 1.6))
            else:
                painter.drawEllipse(QtCore.QRectF(x - r, y - r, 2 * r, 2 * r))
            if label:
                self._halo_text(painter, x + r + 4, y - r - 2, label)

    def _draw_vessel(self, painter: QtGui.QPainter) -> None:
        cache = self._cache
        tr_sl = cache.trail_slice
        if tr_sl is not None:
            px, py, _, valid = self._proj  # type: ignore[misc]
            pen = QtGui.QPen(QtGui.QColor(255, 235, 160, 170), 1.6)
            pen.setStyle(_PEN_STYLE.DashLine)
            painter.setPen(pen)
            painter.setBrush(QtGui.QBrush())
            self._draw_masked_polyline(painter, px[tr_sl], py[tr_sl], valid[tr_sl])
        if cache.vessel_slice is None:
            return
        px, py, _, valid = self._proj  # type: ignore[misc]
        sl = cache.vessel_slice
        if not valid[sl].all():
            return
        base = [QtCore.QPointF(px[i], py[i]) for i in range(sl.start, sl.stop)]
        col = QtGui.QColor(cache.vessel_color)
        outline = QtGui.QPen(QtGui.QColor(235, 240, 245), 1.4)

        deck_sl = cache.vessel_deck_slice
        if deck_sl is not None and valid[deck_sl].all():
            deck = [QtCore.QPointF(px[i], py[i]) for i in range(deck_sl.start, deck_sl.stop)]
            n = len(base)
            side = QtGui.QColor(col).darker(135)
            painter.setPen(QtGui.QPen(QtGui.QColor(235, 240, 245, 120), 0.8))
            painter.setBrush(QtGui.QBrush(side))
            for i in range(n):
                j = (i + 1) % n
                painter.drawPolygon(QtGui.QPolygonF([base[i], base[j], deck[j], deck[i]]))
            painter.setPen(outline)
            painter.setBrush(QtGui.QBrush(col))
            painter.drawPolygon(QtGui.QPolygonF(deck))
            label_pts = deck
        else:
            painter.setPen(outline)
            painter.setBrush(QtGui.QBrush(col))
            painter.drawPolygon(QtGui.QPolygonF(base))
            label_pts = base

        arc_sl = cache.chute_arc_slice
        if arc_sl is not None and valid[arc_sl].all():
            pen = QtGui.QPen(QtGui.QColor(255, 170, 60), 2.4)
            pen.setCapStyle(_PEN_CAP.RoundCap)
            painter.setPen(pen)
            painter.setBrush(QtGui.QBrush())
            arc = QtGui.QPolygonF([QtCore.QPointF(px[i], py[i])
                                   for i in range(arc_sl.start, arc_sl.stop)])
            painter.drawPolyline(arc)

        sv_sl = cache.vessel_sheaves_slice
        if sv_sl is not None and valid[sv_sl].all():
            for k, i in enumerate(range(sv_sl.start, sv_sl.stop)):
                x, y = float(px[i]), float(py[i])
                painter.setPen(QtGui.QPen(QtGui.QColor(20, 26, 36), 1.0))
                painter.setBrush(QtGui.QBrush(QtGui.QColor(255, 200, 90)))
                painter.drawEllipse(QtCore.QRectF(x - 3, y - 3, 6, 6))
                if k < len(cache.vessel_sheave_labels):
                    self._halo_text(painter, x + 5, y - 3,
                                    cache.vessel_sheave_labels[k])

        ch_sl = cache.vessel_chute_slice
        if ch_sl is not None and valid[ch_sl].all():
            # Chute (departure point).
            x, y = float(px[ch_sl.start]), float(py[ch_sl.start])
            painter.setPen(QtGui.QPen(QtGui.QColor(20, 26, 36), 1.0))
            painter.setBrush(QtGui.QBrush(QtGui.QColor(255, 170, 60)))
            painter.drawEllipse(QtCore.QRectF(x - 4, y - 4, 8, 8))
            self._halo_text(painter, x + 6, y - 4,
                            getattr(cache, "departure_label", "chute"))
            # CRP cross.
            if ch_sl.stop - ch_sl.start > 1:
                x, y = float(px[ch_sl.start + 1]), float(py[ch_sl.start + 1])
                painter.setPen(QtGui.QPen(QtGui.QColor(120, 220, 255), 1.6))
                painter.drawLine(QtCore.QPointF(x - 5, y), QtCore.QPointF(x + 5, y))
                painter.drawLine(QtCore.QPointF(x, y - 5), QtCore.QPointF(x, y + 5))
                self._halo_text(painter, x + 6, y + 10, "CRP")

        if cache.vessel_label:
            cx = sum(p.x() for p in label_pts) / len(label_pts)
            cy = min(p.y() for p in label_pts)
            self._halo_text(painter, cx + 6, cy - 6, cache.vessel_label)

    def _draw_hover(self, painter: QtGui.QPainter) -> None:
        if self._hover is None or self._proj is None or self._mouse_pos is None:
            return
        cache = self._cache
        ci, vi = self._hover
        if ci >= len(cache.cable_slices):
            return
        sl = cache.cable_slices[ci]
        px, py, _, valid = self._proj
        gi = sl.start + vi
        if gi >= sl.stop or not valid[gi]:
            return
        x, y = float(px[gi]), float(py[gi])
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 220, 90), 2.0))
        painter.setBrush(QtGui.QBrush())
        painter.drawEllipse(QtCore.QRectF(x - 7, y - 7, 14, 14))
        if self._hover_text:
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(self._hover_text)
            th = fm.height()
            mx, my = self._mouse_pos
            bx = min(max(mx + 14, 4), max(self.width() - tw - 16, 4))
            by = min(max(my - th - 14, 4), max(self.height() - th - 12, 4))
            rect = QtCore.QRectF(bx, by, tw + 12, th + 8)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 220, 90, 140), 1.0))
            painter.setBrush(QtGui.QBrush(QtGui.QColor(12, 18, 28, 215)))
            painter.drawRoundedRect(rect, 4, 4)
            painter.setPen(_HUD_TEXT)
            painter.drawText(QtCore.QPointF(bx + 6, by + 4 + fm.ascent()), self._hover_text)

    # ---------------------------------------------------------------- HUD

    def _halo_text(self, painter: QtGui.QPainter, x: float, y: float, text: str,
                   color: QtGui.QColor = _HUD_TEXT) -> None:
        point = QtCore.QPointF
        painter.setPen(_HUD_HALO)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(point(x + dx, y + dy), text)
        painter.setPen(color)
        painter.drawText(point(x, y), text)

    def _draw_hud(self, painter: QtGui.QPainter) -> None:
        painter.setRenderHint(_RENDER_HINT.Antialiasing, True)
        w, h = self.width(), self.height()
        title = str(getattr(self._scene, "title", "") or "") if self._scene is not None else ""
        if title:
            font = painter.font()
            bold = QtGui.QFont(font)
            bold.setBold(True)
            painter.setFont(bold)
            self._halo_text(painter, 10, 20, title)
            painter.setFont(font)
        zex_text = "z ×%g" % self._zex
        fm = painter.fontMetrics()
        self._halo_text(painter, w - fm.horizontalAdvance(zex_text) - 10, 20, zex_text)
        self._draw_scale_bar(painter)
        self._draw_triad(painter)
        if (self._color_mode == "tension" and self._cache is not None
                and self._cache.tension_range is not None):
            self._draw_tension_legend(painter)

    def _draw_tension_legend(self, painter: QtGui.QPainter) -> None:
        """Vertical colour key for tension colouring (kN, min -> max)."""
        t0, t1 = self._cache.tension_range  # type: ignore[union-attr]
        x0, y1 = 14.0, self.height() - 40.0
        bar_h, bar_w = 90.0, 10.0
        y0 = y1 - bar_h
        grad = QtGui.QLinearGradient(x0, y1, x0, y0)
        n = len(_VIRIDIS_STOPS)
        for i, (r, g, b) in enumerate(_VIRIDIS_STOPS):
            grad.setColorAt(i / (n - 1), QtGui.QColor(int(r), int(g), int(b)))
        painter.setPen(QtGui.QPen(QtGui.QColor(235, 240, 245, 160), 1.0))
        painter.setBrush(QtGui.QBrush(grad))
        painter.drawRect(QtCore.QRectF(x0, y0, bar_w, bar_h))
        self._halo_text(painter, x0 + bar_w + 6, y0 + 4, "%.1f kN" % t1)
        self._halo_text(painter, x0 + bar_w + 6, y1 + 4, "%.1f kN" % t0)
        self._halo_text(painter, x0, y0 - 6, "tension")

    def _draw_scale_bar(self, painter: QtGui.QPainter) -> None:
        mpp = self._metres_per_px()
        if not math.isfinite(mpp) or mpp <= 0:
            return
        metres = _nice_number(mpp * 120.0)
        length_px = metres / mpp
        if length_px > self.width() * 0.5:
            metres = _nice_number(mpp * 60.0)
            length_px = metres / mpp
        x0, y0 = 14.0, self.height() - 16.0
        painter.setPen(QtGui.QPen(_HUD_TEXT, 2.0))
        painter.drawLine(QtCore.QPointF(x0, y0), QtCore.QPointF(x0 + length_px, y0))
        painter.drawLine(QtCore.QPointF(x0, y0 - 4), QtCore.QPointF(x0, y0 + 4))
        painter.drawLine(QtCore.QPointF(x0 + length_px, y0 - 4), QtCore.QPointF(x0 + length_px, y0 + 4))
        label = ("%g m horiz." % metres) if metres < 1000 else ("%g km horiz." % (metres / 1000.0))
        self._halo_text(painter, x0 + length_px + 8, y0 + 4, label)

    def _draw_triad(self, painter: QtGui.QPainter) -> None:
        _, rot = self._camera()
        ox, oy = self.width() - 48.0, self.height() - 44.0
        axes = (
            (np.array([1.0, 0.0, 0.0]), QtGui.QColor(235, 110, 100), "x"),
            (np.array([0.0, 1.0, 0.0]), QtGui.QColor(120, 210, 120), "y"),
            (np.array([0.0, 0.0, 1.0]), QtGui.QColor(120, 180, 245), "z"),
        )
        for vec, color, name in axes:
            sx = float(np.dot(vec, rot[0]))
            sy = -float(np.dot(vec, rot[1]))
            ex, ey = ox + sx * 26.0, oy + sy * 26.0
            painter.setPen(QtGui.QPen(color, 2.0))
            painter.drawLine(QtCore.QPointF(ox, oy), QtCore.QPointF(ex, ey))
            self._halo_text(painter, ex + sx * 6.0 - 3.0, ey + sy * 6.0 + 4.0, name, color)

    # --------------------------------------------------------- interaction

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        x, y = _event_xy(event)
        self._press_pos = (x, y)
        self._drag_last = (x, y)
        self._drag_total = 0.0
        self._drag_button = event.button()
        self._orbit_pivot = (self._pivot_under_cursor(x, y)
                             if self._drag_button == _MOUSE_BUTTON.LeftButton else None)
        event.accept()

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        x, y = _event_xy(event)
        self._mouse_pos = (x, y)
        if self._drag_button is not None and (event.buttons() & self._drag_button):
            dx = x - self._drag_last[0]
            dy = y - self._drag_last[1]
            self._drag_last = (x, y)
            self._drag_total += abs(dx) + abs(dy)
            if self._drag_button == _MOUSE_BUTTON.LeftButton:
                self._orbit(-dx * 0.4, -dy * 0.4)
            elif self._drag_button in (_MOUSE_BUTTON.MiddleButton, _MOUSE_BUTTON.RightButton):
                self._pan(dx, dy)
            self.update()
        else:
            self._update_hover(x, y)
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        x, y = _event_xy(event)
        if (self._press_pos is not None and event.button() == _MOUSE_BUTTON.LeftButton
                and self._drag_total < _DRAG_THRESHOLD_PX):
            hit = self._pick(x, y)
            if hit is not None:
                self.pointPicked.emit(hit[0], hit[1])
        self._drag_button = None
        self._press_pos = None
        self._orbit_pivot = None
        event.accept()

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        self._yaw = _DEFAULT_YAW_DEG
        self._pitch = _DEFAULT_PITCH_DEG
        self._press_pos = None
        self._drag_button = None
        self._orbit_pivot = None
        self.fit_view()
        event.accept()

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        factor = math.pow(0.9, delta / 120.0)
        mx, my = _event_xy(event)
        mpp = self._metres_per_px()
        _, rot = self._camera()
        off = (rot[0] * (mx - self.width() * 0.5) - rot[1] * (my - self.height() * 0.5))
        off = off * mpp * (1.0 - factor)
        self._target = self._target + np.array([off[0], off[1], off[2] / self._zex])
        self._distance = min(max(self._distance * factor, 0.1), 1e8)
        self.update()
        event.accept()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802
        self._mouse_pos = None
        if self._hover is not None:
            self._hover = None
            self._hover_text = ""
            self.hoverInfo.emit("")
            self.update()
        super().leaveEvent(event)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        self._proj = None
        self._layout_nav_buttons()
        super().resizeEvent(event)

    # ------------------------------------------------------------- picking

    def _pick(self, mx: float, my: float) -> Optional[Tuple[int, int]]:
        if self._proj is None or self._cache is None:
            return None
        px, py, _, valid = self._proj
        best: Optional[Tuple[int, int]] = None
        best_d2 = _PICK_RADIUS_PX * _PICK_RADIUS_PX
        for ci, sl in enumerate(self._cache.cable_slices):
            if sl.stop <= sl.start:
                continue
            d2 = (px[sl] - mx) ** 2 + (py[sl] - my) ** 2
            d2 = np.where(valid[sl], d2, np.inf)
            vi = int(np.argmin(d2))
            if d2[vi] < best_d2:
                best_d2 = float(d2[vi])
                best = (ci, vi)
        return best

    def _update_hover(self, mx: float, my: float) -> None:
        hit = self._pick(mx, my)
        if hit == self._hover:
            if hit is not None:
                self.update()   # keep the tooltip box tracking the cursor
            return
        self._hover = hit
        text = self._hover_readout(hit) if hit is not None else ""
        self._hover_text = text
        self.hoverInfo.emit(text)
        self.update()

    def _hover_readout(self, hit: Tuple[int, int]) -> str:
        ci, vi = hit
        data = self._cache.cable_data[ci]  # type: ignore[union-attr]
        parts = [data["name"]]
        s = data["s"]
        if s is not None and vi < len(s) and np.isfinite(float(s[vi])):
            parts.append("s=%.1f m" % float(s[vi]))
        z = float(data["xyz"][vi, 2])
        parts.append("depth=%.1f m" % -z)
        t = data["tension"]
        if t is not None and vi < len(t) and np.isfinite(float(t[vi])):
            parts.append("T=%.1f kN" % float(t[vi]))
        contact = data["contact"]
        if contact is not None and vi < len(contact) and bool(contact[vi]):
            parts.append("on bed")
        return " | ".join(parts)

    def _pan(self, dx: float, dy: float) -> None:
        mpp = self._metres_per_px()
        _, rot = self._camera()
        off = (-rot[0] * dx + rot[1] * dy) * mpp
        self._target = self._target + np.array([off[0], off[1], off[2] / self._zex])
