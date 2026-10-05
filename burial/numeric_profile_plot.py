"""One graphics item for numeric profiles; raster work scales with screen height.

Raw samples remain indexed for hover and inspection. A cached one-pixel-wide
strip per visible source is sampled at display pixel centres, without blending.
KP panning reuses strips; depth zoom rebuilds strips but never colour limits.
"""
import bisect

import numpy as np
import pyqtgraph as pg
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QImage

RAMPS = {
    "Viridis": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    "Plasma": ["#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"],
    "Blue–red": ["#2166ac", "#67a9cf", "#f7f7f7", "#ef8a62", "#b2182b"],
    "Red–yellow–green": ["#d7191c", "#fdae61", "#ffffbf", "#a6d96a", "#1a9641"],
    "Greys": ["#f7f7f7", "#252525"],
}


_EPS = 1e-12  # as burial.attribute_rules
# Values in a gap between custom classes: distinct from missing (light grey).
OUTSIDE_CLASSES = (64, 64, 64, 255)


def ramp_colours(ramp, count):
    """``count`` hex colours spread along a ramp (for new custom classes)."""
    table = colour_table(ramp)
    picks = np.linspace(0, 255, count).round().astype(int) if count > 1 else [0]
    return ["#{:02x}{:02x}{:02x}".format(*table[i][:3]) for i in picks]


def class_arrays(classes):
    """Bounds, bound inclusivity and RGBA rows, in class (row) order."""
    lows = np.array([-np.inf if c["min"] is None else c["min"] for c in classes], dtype=float)
    highs = np.array([np.inf if c["max"] is None else c["max"] for c in classes], dtype=float)
    low_in = np.array([c["min_inclusive"] for c in classes], dtype=bool)
    high_in = np.array([c["max_inclusive"] for c in classes], dtype=bool)
    colours = np.array([QColor(c["colour"]).getRgb() for c in classes], dtype=np.uint8)
    return lows, highs, low_in, high_in, colours


def classify(values, arrays):
    """RGBA per value; the first matching class wins (as attribute rules)."""
    lows, highs, low_in, high_in, colours = arrays
    result = np.empty((len(values), 4), dtype=np.uint8)
    result[:] = OUTSIDE_CLASSES
    pending = np.isfinite(values)
    with np.errstate(invalid="ignore"):
        for i in range(len(lows)):
            above = values >= lows[i] - _EPS if low_in[i] else values > lows[i] + _EPS
            below = values <= highs[i] + _EPS if high_in[i] else values < highs[i] - _EPS
            hit = pending & above & below
            result[hit] = colours[i]
            pending &= ~hit
    return result


def colour_table(ramp, bands=0):
    colors = [QColor(c).getRgb() for c in RAMPS.get(ramp, RAMPS["Viridis"])]
    cmap = pg.ColorMap(np.linspace(0, 1, len(colors)), colors)
    positions = np.linspace(0, 1, 256)
    if bands:
        positions = np.minimum((positions * bands).astype(int), bands - 1) / max(1, bands - 1)
    return cmap.map(positions, mode="byte")


class NumericProfileItem(pg.GraphicsObject):
    def __init__(self):
        super().__init__()
        self.index = None
        self.classes = None
        self._cache = {}
        self._depth_key = None
        self._arrays = {}
        style = getattr(Qt, "BrushStyle", Qt)
        self._conflict = QBrush(QColor("#d18b30"), style.DiagCrossPattern)
        self._missing = QBrush(QColor("#bbbbbb"), style.BDiagPattern)

    def set_profiles(self, index, limits, ramp, bands, classes=None):
        self.prepareGeometryChange()
        self.index = index
        self.limits = limits
        self.lut = colour_table(ramp, bands)
        self.classes = class_arrays(classes) if classes else None
        self._cache.clear()
        self._depth_key = None
        self._arrays = {}
        for source, profile in index.profiles.items():
            samples = profile["samples"]
            self._arrays[source] = (
                np.array([s["top"] for s in samples]), np.array([s["base"] for s in samples]),
                np.array([np.nan if s["value"] is None else s["value"] for s in samples]),
                np.array([bool(s["flags"]) for s in samples]))
        self.update()

    def boundingRect(self):
        if not self.index or not self.index.runs:
            return QRectF()
        depth = max((p["samples"][-1]["base"] for p in self.index.profiles.values() if p["samples"]), default=3)
        return QRectF(self.index.runs[0][0], 0,
                      self.index.runs[-1][1] - self.index.runs[0][0], depth)

    def _strip(self, source, top, bottom, height):
        if source in self._cache:
            return self._cache[source]
        starts, ends, values, flags = self._arrays[source]
        if not len(starts):
            return None
        probes = top + (np.arange(height) + .5) * (bottom - top) / height
        indices = np.searchsorted(starts, probes, side="right") - 1
        safe = np.maximum(indices, 0)
        covered = (indices >= 0) & (probes < ends[safe])
        measured = covered & np.isfinite(values[safe])
        if self.classes is not None:
            colors = classify(values[safe], self.classes)
        else:
            lo, hi = self.limits
            normalized = np.nan_to_num((values[safe] - lo) / (hi - lo), nan=0.0)
            colors = self.lut[np.clip(normalized * 255, 0, 255).astype(int)].copy()
        colors[~measured] = (0, 0, 0, 0)
        colors[covered & ~measured] = (180, 180, 180, 255)
        # Retain partial/quality flags visibly without changing the value colour.
        rgba = np.repeat(colors[:, None, :], 8, axis=1).astype(np.uint8)
        marked = covered & (flags[safe] | ~measured)
        for col in range(8):
            rgba[marked & ((np.arange(height) + col) % 8 < 2), col] = (100, 100, 100, 255)
        fmt = getattr(QImage, "Format", QImage).Format_RGBA8888
        image = QImage(rgba.data, 8, height, rgba.strides[0], fmt).copy()
        self._cache[source] = image
        return image

    def paint(self, painter, *_args):
        if not self.index or not self.index.runs:
            return
        view = self.viewRect()
        if view is None:
            return
        top, bottom = max(0.0, view.top()), view.bottom()
        if bottom <= top:
            return
        height = min(4096, max(1, int(self.getViewBox().height())))
        key = (top, bottom, height)
        if key != self._depth_key:
            self._cache.clear()
            self._depth_key = key
        painter.setPen(pg.mkPen(None))
        first = max(0, bisect.bisect_right(self.index.starts, view.left()) - 1)
        for i in range(first, len(self.index.runs)):
            a, b, active = self.index.runs[i]
            if a > view.right():
                break
            if b < view.left():
                continue
            rect = QRectF(max(a, view.left()), top, min(b, view.right()) - max(a, view.left()), bottom - top)
            source = self.index.assignments[active[0]]["source_id"]
            if len(active) > 1 or source not in self._arrays:
                painter.fillRect(rect, self._conflict if len(active) > 1 else self._missing)
            else:
                image = self._strip(source, top, bottom, height)
                if image is not None:
                    painter.drawImage(rect, image)
