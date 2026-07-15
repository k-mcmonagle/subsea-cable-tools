# -*- coding: utf-8 -*-
"""2D profile and plan views for the Cable Lay Simulator.

Both widgets consume the same :class:`SceneData` as the 3D viewport, via the
plugin's pyqtgraph-backed matplotlib-style plot shim (so SVG export and the
familiar V2 look come for free).

* Profile view — the cable *unrolled*: elevation vs arc length along each
  chain, with the seabed elevation directly beneath each cable point. This
  stays meaningful for fully 3D geometries (BU legs, bights) where a single
  vertical-plane profile does not exist.
* Plan view — x/y geometry with the vessel, markers and bed extent, equal
  aspect.
"""

from __future__ import annotations

import math
from typing import List, Optional

import numpy as np


def _load_shim():
    """Import the plugin's pyqtgraph-backed plot shim.

    Inside QGIS the plugin package import works; in standalone tests fall
    back to loading ``plot_widget.py`` from the plugin root by path."""
    from importlib import import_module

    try:
        # Same shim the V2 dialog uses. __package__ is e.g.
        # 'subsea_cable_tools.catenary.v3.ui' -> plugin root package.
        parent_pkg = __package__.rsplit(".", 3)[0] if __package__ else ""
        if parent_pkg and parent_pkg != __package__:
            return import_module(parent_pkg + ".plot_widget")
    except Exception:
        pass
    import importlib.util
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.path.join(root, "plot_widget.py")
    spec = importlib.util.spec_from_file_location("sct_plot_widget_shim", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sct_plot_widget_shim"] = mod
    spec.loader.exec_module(mod)
    return mod


class _ShimHolder:
    _mod = None

    @classmethod
    def mod(cls):
        if cls._mod is None:
            cls._mod = _load_shim()
        return cls._mod


def make_canvas():
    shim = _ShimHolder.mod()
    fig = shim.Figure(figsize=(6, 4))
    canvas = shim.FigureCanvas(fig)
    return fig, canvas


class ProfileView:
    """Unrolled elevation-vs-arc-length profile of every chain."""

    def __init__(self, bathy_lookup=None):
        self.figure, self.canvas = make_canvas()
        self._bathy_lookup = bathy_lookup  # callable (x_arr, y_arr) -> depth_arr
        self._equal_aspect = True
        self._last_scene = None

    def widget(self):
        return self.canvas

    def set_bathy_lookup(self, fn):
        self._bathy_lookup = fn

    def set_equal_aspect(self, on: bool) -> None:
        on = bool(on)
        if on == self._equal_aspect:
            return
        self._equal_aspect = on
        self.update_scene(self._last_scene)

    def update_scene(self, scene) -> None:
        self._last_scene = scene
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        if scene is None or not scene.cables:
            ax.set_title("No solution")
            self.canvas.draw()
            return
        shim = _ShimHolder.mod()
        offset = 0.0
        for i, path in enumerate(scene.cables):
            xyz = np.asarray(path.xyz, dtype=float)
            if len(xyz) < 2:
                continue
            if path.s_m is not None:
                s = np.asarray(path.s_m, dtype=float)
            else:
                seg = np.linalg.norm(np.diff(xyz, axis=0), axis=1)
                s = np.concatenate([[0.0], np.cumsum(seg)])
            s_plot = s + offset
            color = path.color or shim.get_tab10_color(i)
            ax.plot(s_plot, xyz[:, 2], color=color, linewidth=2.0, label=path.name)
            # Bed under the cable.
            if self._bathy_lookup is not None:
                try:
                    depth = np.asarray(self._bathy_lookup(xyz[:, 0], xyz[:, 1]), dtype=float)
                    ax.plot(s_plot, -depth, color="#8c6d31", linewidth=1.0,
                            linestyle="--", label="_nolegend_")
                except Exception:
                    pass
            offset = float(s_plot[-1]) + max(10.0, 0.02 * float(s_plot[-1]))
        ax.axhline(0.0, color="#7fb2d9", linewidth=1.0, linestyle=":")
        ax.set_xlabel("Distance along cable (m)")
        ax.set_ylabel("Elevation (m, 0 = sea surface)")
        try:
            ax.set_aspect("equal" if self._equal_aspect else "auto")
        except Exception:
            pass
        try:
            ax.legend()
        except Exception:
            pass
        self.canvas.draw()


class PlanView:
    """Top-down x/y geometry with vessel and markers."""

    def __init__(self):
        self.figure, self.canvas = make_canvas()

    def widget(self):
        return self.canvas

    def update_scene(self, scene) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        if scene is None or not scene.cables:
            ax.set_title("No solution")
            self.canvas.draw()
            return
        shim = _ShimHolder.mod()
        if scene.bed is not None:
            x = np.asarray(scene.bed.x, dtype=float)
            y = np.asarray(scene.bed.y, dtype=float)
            bx = [x[0], x[-1], x[-1], x[0], x[0]]
            by = [y[0], y[0], y[-1], y[-1], y[0]]
            ax.plot(bx, by, color="#999999", linewidth=0.8, linestyle=":",
                    label="_nolegend_")
        for i, path in enumerate(scene.cables):
            xyz = np.asarray(path.xyz, dtype=float)
            if len(xyz) < 2:
                continue
            color = path.color or shim.get_tab10_color(i)
            ax.plot(xyz[:, 0], xyz[:, 1], color=color, linewidth=2.0, label=path.name)
            if path.contact is not None and np.any(path.contact):
                on = np.asarray(path.contact, dtype=bool)
                ax.scatter(xyz[on, 0], xyz[on, 1], s=6, color=color, alpha=0.4,
                           label="_nolegend_")
        for m in scene.markers:
            if not np.all(np.isfinite(np.asarray(m.xyz[:2], dtype=float))):
                continue
            ax.scatter([m.xyz[0]], [m.xyz[1]], s=30, color=m.color, label="_nolegend_")
            if m.label:
                ax.text(m.xyz[0], m.xyz[1], " " + m.label, fontsize=8)
        if scene.vessel is not None and np.all(np.isfinite(np.asarray(scene.vessel.xy, dtype=float))):
            from .scene import vessel_footprint

            pv = vessel_footprint(scene.vessel)
            pv = np.vstack([pv, pv[:1]])  # close the outline
            ax.plot(pv[:, 0], pv[:, 1], color="#444444", linewidth=1.5, label="_nolegend_")
            ax.scatter([scene.vessel.xy[0]], [scene.vessel.xy[1]], s=18,
                       color="#ffaa3c", label="_nolegend_", zorder=5)
        ax.set_xlabel("x (m)")
        ax.set_ylabel("y (m)")
        try:
            ax.set_aspect("equal")
        except Exception:
            pass
        try:
            ax.legend()
        except Exception:
            pass
        self.canvas.draw()
