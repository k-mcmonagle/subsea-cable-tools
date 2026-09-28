"""pyqtgraph bottom axis with round-KP ticks labelled to 3 dp.

Modes (see :mod:`kp_axis`):

* ``set_linear(km_per_unit, offset_km)`` — plot X converts linearly to KP.
* ``set_mapped(crossings, metres_per_unit)`` — plot X is distance along a
  line; ticks land where the line's nearest route KP crosses round values.
* ``set_plain()`` — pyqtgraph's default ticks and labels.

Mapped mode falls back to the default ticks, labelled with the nearest KP,
when the visible stretch crosses fewer than two round KPs (a line running
across the route barely changes KP).
"""
import pyqtgraph as pg

from .kp_axis import MIN_LABEL_PX, format_kp, linear_kp_ticks


class KPAxisItem(pg.AxisItem):
    def __init__(self, orientation='bottom', **kwargs):
        super().__init__(orientation=orientation, **kwargs)
        self._mode = 'plain'
        self._km_per_unit = 1.0
        self._offset_km = 0.0
        self._crossings = None
        self._m_per_unit = 1.0
        self._labels = {}
        self.enableAutoSIPrefix(False)

    # ------------------------------------------------------------ configure
    def set_plain(self):
        self._mode = 'plain'
        self._crossings = None
        self._invalidate()

    def set_linear(self, km_per_unit=1.0, offset_km=0.0):
        self._mode = 'linear'
        self._km_per_unit = float(km_per_unit)
        self._offset_km = float(offset_km)
        self._invalidate()

    def set_mapped(self, crossings, metres_per_unit=1.0):
        """``crossings``: a :class:`kp_axis.KPCrossings` over metres."""
        if not crossings:
            self.set_plain()
            return
        self._mode = 'mapped'
        self._crossings = crossings
        self._m_per_unit = float(metres_per_unit) or 1.0
        self._invalidate()

    @property
    def mode(self):
        return self._mode

    def kp_at(self, x):
        """Route KP (km) at plot X, or None (plain mode / off the route)."""
        if self._mode == 'linear':
            return self._offset_km + x * self._km_per_unit
        if self._mode == 'mapped' and self._crossings is not None:
            return self._crossings.kp_at(x * self._m_per_unit)
        return None

    def _invalidate(self):
        self._labels = {}
        self.picture = None
        self.update()

    # ------------------------------------------------------------ pyqtgraph
    def tickValues(self, minVal, maxVal, size):
        minVal, maxVal = sorted((minVal, maxVal))
        self._labels = {}
        if self._mode == 'linear':
            step, positions = linear_kp_ticks(minVal, maxVal, size, self._km_per_unit,
                                              self._offset_km, MIN_LABEL_PX)
            if positions:
                return [(step, positions)]
        elif self._mode == 'mapped' and self._crossings is not None and maxVal > minVal:
            m = self._m_per_unit
            px_per_m = size / ((maxVal - minVal) * m) if size else 0.0
            _step, ticks = self._crossings.ticks(minVal * m, maxVal * m, px_per_m)
            if len(ticks) >= 2:
                positions = []
                for x_m, kp in ticks:
                    pos = x_m / m
                    positions.append(pos)
                    self._labels[self._key(pos)] = format_kp(kp)
                spacing = (positions[-1] - positions[0]) / max(1, len(positions) - 1)
                return [(spacing, positions)]
        return super().tickValues(minVal, maxVal, size)

    @staticmethod
    def _key(value):
        return round(float(value), 9)

    def tickStrings(self, values, scale, spacing):
        if self._mode == 'plain':
            return super().tickStrings(values, scale, spacing)
        out = []
        for value in values:
            label = self._labels.get(self._key(value))
            if label is None:
                # Fallback ticks (too little KP change for round crossings):
                # label the nearest KP at the tick instead.
                label = format_kp(self.kp_at(value))
            out.append(label)
        return out
