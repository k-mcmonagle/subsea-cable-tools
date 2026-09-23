"""Two-click on-plot measurements for pyqtgraph depth profiles.

Shared by the KP Mouse quick profile and the Depth Profile dock. The host
supplies the plot item, the measurable series (metres, positive-down depth)
and how plot coordinates relate to metres; the controller owns the Measure
action, snapping, the elastic preview, draggable endpoints and the table.
"""
import pyqtgraph as pg
from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                                 QPushButton, QTableWidget, QTableWidgetItem)
from ..qgis_compat import QAction
from ..slope_utils import interpolate_covered, is_finite
from .profile_measurements import UNITS, measurement

try:
    from qgis.PyQt import sip
    _deleted = sip.isdeleted
except Exception:  # pragma: no cover
    def _deleted(_obj):
        return False

_COLOR = '#c2185b'
_LABEL_COLOR = '#8e1243'
COLUMNS = ['Length', 'X', 'Y', 'Angle (°)', 'Along seabed']
HINT = ('Click two points; drag to pan; scroll to zoom. Escape cancels; '
        'Delete removes a measurement; drag an endpoint to adjust it.')


class ProfileMeasureController(QObject):
    """Measurements on one depth plot item.

    ``plot_factors()`` returns (x, z) plot units per metre; ``text_units()``
    returns the (horizontal, vertical) unit names used for labels and the
    table. ``depth_down()`` / ``x_inverted()`` describe screen orientation
    so labels sit outside the measurement triangle.
    """
    modeChanged = pyqtSignal(bool)
    statusChanged = pyqtSignal(str)
    measurementsChanged = pyqtSignal()

    def __init__(self, parent=None, plot_factors=None, text_units=None,
                 depth_down=None, x_inverted=None):
        super().__init__(parent)
        self._plot_factors = plot_factors or (lambda: (1.0, 1.0))
        self._text_units = text_units or (lambda: ('m', 'm'))
        self._depth_down = depth_down or (lambda: True)
        self._x_inverted = x_inverted or (lambda: False)
        self.plot_item = None
        self._scene = None
        self.series = []
        self.measurements = []
        self.first_point = None
        self.preview = None
        self.graphics = []
        self._pending_marker = None
        self._moving_endpoint = False

        self.action = QAction('Measure', self)
        self.action.setCheckable(True)
        self.action.setToolTip('Click two endpoints on the depth plot; drag to pan and scroll to zoom. '
                               'Uncheck to stop measuring.')
        self.action.toggled.connect(self._mode_toggled)
        self.snap_check = QCheckBox('Snap to profile')
        self.snap_check.setChecked(True)
        self.snap_check.setToolTip('Place endpoints on the selected profile line. Snapped measurements also '
                                   'report the distance traced along the seabed between the endpoints.')
        self.snap_check.toggled.connect(self.cancel_pending)
        self.source_combo = QComboBox()
        self.source_combo.setToolTip('Profile line that snapped endpoints follow')
        self.source_combo.currentIndexChanged.connect(self.cancel_pending)
        self.delete_btn = QPushButton('Delete / undo')
        self.delete_btn.setToolTip('Remove the selected measurement (or the last one)')
        self.delete_btn.clicked.connect(self.delete_measurement)
        self.clear_btn = QPushButton('Clear measurements')
        self.clear_btn.clicked.connect(self.clear)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setToolTip(
            'Length: straight line between endpoints (horizontal units). X: horizontal separation. '
            'Y: absolute depth difference (vertical units). Angle: unsigned endpoint inclination to '
            'horizontal (0–90°), not the maximum seabed slope between points. Along seabed: distance '
            'traced along the snapped profile (blank when not snapped or across a gap). Measurements '
            'use true distances, regardless of vertical exaggeration or KP labels.')
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(145)

    # ------------------------------------------------------------ host API
    @property
    def active(self):
        return self.action.isChecked()

    def attach(self, plot_item, series=None, reset=True):
        """Measure on ``plot_item`` (None detaches). ``series`` replaces the
        snap targets; ``reset`` discards measurements from earlier data."""
        self._detach_scene()
        self.plot_item = plot_item
        self.graphics, self.preview, self._pending_marker = [], None, None
        if reset:
            self.reset()
        if plot_item is not None:
            scene = plot_item.scene()
            scene.sigMouseClicked.connect(self._on_click)
            scene.sigMouseMoved.connect(self._on_move)
            self._scene = scene
            menu = plot_item.vb.getMenu(None)
            if self.action not in menu.actions():
                menu.addAction(self.action)
        if series is not None:
            self.set_series(series)
        else:
            self.refresh()

    def set_series(self, series):
        """Replace the measurable series: [{'name', 'x', 'y', 'sources'?}],
        x ascending in metres, y metres positive down."""
        self.series = list(series or [])
        previous = self.source_combo.currentText()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for item in self.series:
            self.source_combo.addItem(item['name'])
        if previous:
            index = self.source_combo.findText(previous)
            if index >= 0:
                self.source_combo.setCurrentIndex(index)
        self.source_combo.blockSignals(False)
        self.refresh()

    def reset(self):
        """Forget measurements without drawing (the plot is being rebuilt)."""
        had = bool(self.measurements or self.first_point)
        self.measurements, self.first_point = [], None
        self.table.setRowCount(0)
        if had:
            self.measurementsChanged.emit()

    def clear(self):
        self.reset()
        self.refresh()

    def cancel_pending(self, *_):
        if self.first_point is not None:
            self.first_point = None
            self.refresh()

    def delete_measurement(self):
        if self.first_point is not None:
            self.first_point = None
        elif self.measurements:
            row = self.table.currentRow()
            self.measurements.pop(row if 0 <= row < len(self.measurements) else -1)
            self.measurementsChanged.emit()
        self.refresh()

    def handle_key(self, event):
        """Delete / Escape handling; True when the key was consumed."""
        key = event.key()
        if key == Qt.Key.Key_Delete and (self.measurements or self.first_point):
            self.delete_measurement()
            return True
        if key == Qt.Key.Key_Escape and self.first_point is not None:
            self.cancel_pending()
            return True
        if key == Qt.Key.Key_Escape and self.active:
            self.action.setChecked(False)
            return True
        return False

    def add_measurement(self, a, b, source_index=None, snapped=None):
        """Add a measurement between two (x_m, depth_m) points."""
        index = self.source_combo.currentIndex() if source_index is None else source_index
        snapped = self.snap_check.isChecked() if snapped is None else snapped
        series = self.series[index] if 0 <= index < len(self.series) else None
        if snapped and series is not None:
            a = (a[0], self._snap(series, a[0]))
            b = (b[0], self._snap(series, b[0]))
            if not is_finite(a[1]) or not is_finite(b[1]):
                return None
        entry = {'a': a, 'b': b, 'source': series['name'] if series else '',
                 'source_index': index, 'snapped': bool(snapped and series is not None),
                 'metrics': self._metrics(a, b, series, snapped)}
        self.measurements.append(entry)
        self.first_point = None
        self.refresh()
        self.measurementsChanged.emit()
        return entry

    def refresh(self):
        """Redraw measurement graphics and the table on the attached plot."""
        self._remove_items()
        self._fill_table()
        if self.plot_item is None or _deleted(self.plot_item):
            return
        xf, zf = self._plot_factors()
        for row, m in enumerate(self.measurements):
            triangle = self._make_triangle()
            handles = []
            for endpoint, point in enumerate((m['a'], m['b'])):
                handle = pg.TargetItem(pos=(point[0] * xf, point[1] * zf), size=11, symbol='o',
                                       movable=True, pen=pg.mkPen(_COLOR), brush=pg.mkBrush('#ffffff'),
                                       hoverPen=pg.mkPen('#1565c0', width=2))
                handle.setZValue(100)
                self.plot_item.addItem(handle, ignoreBounds=True)
                handle.sigPositionChanged.connect(
                    lambda target, r=row, e=endpoint: self._move_endpoint(r, e, target))
                handles.append(handle)
            self.graphics.append((triangle, handles))
            self._update_triangle(triangle, m['a'], m['b'], m['metrics'], row + 1)
        if self.first_point is not None:
            (x, z), _, _ = self.first_point
            self._pending_marker = pg.ScatterPlotItem([x * xf], [z * zf], symbol='o', size=9,
                                                      pen=pg.mkPen(None), brush=pg.mkBrush(_COLOR))
            self._pending_marker.setZValue(100)
            self.plot_item.addItem(self._pending_marker, ignoreBounds=True)

    def metric_text(self, values):
        """(length, x, y) strings in the current text units."""
        x_unit, z_unit = self._text_units()
        return ['%.3f %s' % (values[key] * UNITS[unit], unit) for key, unit in
                (('endpoint_distance_m', x_unit), ('width_m', x_unit), ('height_m', z_unit))]

    def row_text(self, values):
        x_unit, _ = self._text_units()
        seabed = values.get('seabed_distance_m')
        return self.metric_text(values) + [
            self._angle_text(values),
            '—' if seabed is None else '%.3f %s' % (seabed * UNITS[x_unit], x_unit)]

    # ------------------------------------------------------------ internals
    def _detach_scene(self):
        scene, self._scene = self._scene, None
        if scene is None or _deleted(scene):
            return
        for signal, slot in ((scene.sigMouseClicked, self._on_click),
                             (scene.sigMouseMoved, self._on_move)):
            try:
                signal.disconnect(slot)
            except Exception:
                pass

    def _remove_items(self):
        items = []
        for triangle, handles in self.graphics:
            items.extend(triangle)
            items.extend(handles)
        if self.preview:
            items.extend(self.preview)
        if self._pending_marker is not None:
            items.append(self._pending_marker)
        if self.plot_item is not None and not _deleted(self.plot_item):
            for item in items:
                try:
                    self.plot_item.removeItem(item)
                except Exception:
                    pass
        self.graphics, self.preview, self._pending_marker = [], None, None

    def _fill_table(self):
        self.table.setRowCount(len(self.measurements))
        for row, m in enumerate(self.measurements):
            tip = '%d: %s%s' % (row + 1, m['source'] or 'free', ' (snapped)' if m['snapped'] else '')
            for col, value in enumerate(self.row_text(m['metrics'])):
                cell = QTableWidgetItem(value)
                cell.setToolTip(tip)
                self.table.setItem(row, col, cell)

    def _mode_toggled(self, checked):
        self.first_point = None
        if self.plot_item is not None and not _deleted(self.plot_item):
            self.plot_item.vb.setMouseEnabled(x=True, y=True)
        self.refresh()
        self.modeChanged.emit(bool(checked))

    @staticmethod
    def _snap(series, x):
        return interpolate_covered(series['x'], series['y'], x, series.get('sources'))

    @staticmethod
    def _metrics(a, b, series, snapped):
        use = snapped and series is not None
        return measurement(a, b, series['x'] if use else None, series['y'] if use else None)

    def _view_point(self, scene_pos):
        xf, zf = self._plot_factors()
        point = self.plot_item.vb.mapSceneToView(scene_pos)
        return point.x() / xf, point.y() / zf

    def _on_click(self, event):
        if (not self.active or self.plot_item is None or _deleted(self.plot_item)
                or event.button() != Qt.MouseButton.LeftButton):
            return
        # Pyqtgraph emits clicks separately from drags. Ignore clicks already
        # consumed by plot items (endpoint handles) and navigation double-clicks.
        if getattr(event, 'isAccepted', lambda: False)() or getattr(event, 'double', lambda: False)():
            return
        if not self.plot_item.vb.sceneBoundingRect().contains(event.scenePos()):
            return
        index = self.source_combo.currentIndex()
        if not 0 <= index < len(self.series):
            self.statusChanged.emit('No profile available to measure.')
            return
        x, z = self._view_point(event.scenePos())
        series = self.series[index]
        snapped = self.snap_check.isChecked()
        if snapped:
            z = self._snap(series, x)
            if z is None:
                self.statusChanged.emit('No supported profile at that position. Choose a point within coverage.')
                return
        if not is_finite(x) or not is_finite(z):
            return
        event.accept()
        if self.first_point is None:
            self.first_point = ((x, z), index, snapped)
            self.refresh()
            self.statusChanged.emit('First point placed. Click the second point; Escape cancels.')
            return
        a, first_index, first_snapped = self.first_point
        if first_index != index or first_snapped != snapped:
            self.first_point = None
            self.refresh()
            self.statusChanged.emit('Source or snapping changed. Place the first point again.')
            return
        entry = self.add_measurement(a, (x, z), index, snapped)
        if entry is not None:
            self.statusChanged.emit('Measurement %d: %s, angle %s.' % (
                len(self.measurements), self.metric_text(entry['metrics'])[0],
                self._angle_text(entry['metrics'])))

    def _hide_preview(self):
        if self.preview:
            for item in self.preview:
                item.hide()

    def _on_move(self, position):
        if not self.first_point or not self.active or self.plot_item is None or _deleted(self.plot_item):
            return
        a, index, snapped = self.first_point
        if (not self.plot_item.vb.sceneBoundingRect().contains(position)
                or index != self.source_combo.currentIndex()
                or snapped != self.snap_check.isChecked()
                or not 0 <= index < len(self.series)):
            self._hide_preview()
            return
        x, z = self._view_point(position)
        if snapped:
            z = self._snap(self.series[index], x)
        if not is_finite(x) or not is_finite(z):
            self._hide_preview()
            return
        if self.preview is None:
            self.preview = self._make_triangle(preview=True)
        self._update_triangle(self.preview, a, (x, z), measurement(a, (x, z)))

    def _make_triangle(self, preview=False):
        diagonal = pg.PlotDataItem(pen=pg.mkPen(_COLOR, width=2,
                                   style=Qt.PenStyle.DashLine if preview else Qt.PenStyle.SolidLine))
        legs = pg.PlotDataItem(pen=pg.mkPen(_COLOR, width=1, style=Qt.PenStyle.DashLine))
        labels = [pg.TextItem(color=_LABEL_COLOR, anchor=anchor, fill=pg.mkBrush(255, 255, 255, 210))
                  for anchor in ((.5, 1), (.5, 0), (0, .5))]
        items = (diagonal, legs, *labels)
        for item in items:
            self.plot_item.addItem(item, ignoreBounds=True)
        return items

    @staticmethod
    def _angle_text(values):
        angle = values.get('angle_deg')
        return '—' if angle is None else '%.2f°' % angle

    def _update_triangle(self, items, a, b, values, number=None):
        xf, zf = self._plot_factors()
        ax, az, bx, bz = a[0] * xf, a[1] * zf, b[0] * xf, b[1] * zf
        diagonal, legs, length_label, x_label, y_label = items
        diagonal.setData([ax, bx], [az, bz])
        # Right-angle corner is (b.x, a.z); legs retain physical X/Y meaning.
        legs.setData([ax, bx, bx], [az, az, bz])
        length, x, y = self.metric_text(values)
        prefix = '%d: ' % number if number is not None else ''
        # Keep labels outside the triangle so shallow slopes do not stack the
        # X and diagonal text; respect depth- and distance-axis inversion.
        down_on_screen = (bz - az) * (1 if self._depth_down() else -1) >= 0
        right_on_screen = (bx >= ax) != bool(self._x_inverted())
        x_label.setAnchor((.5, 1 if down_on_screen else 0))
        length_label.setAnchor((.5, 0 if down_on_screen else 1))
        y_label.setAnchor((0 if right_on_screen else 1, .5))
        length_label.setText(prefix + 'Length ' + length + ' / ' + self._angle_text(values))
        length_label.setPos((ax + bx) / 2, (az + bz) / 2)
        x_label.setText('X ' + x)
        x_label.setPos((ax + bx) / 2, az)
        y_label.setText('Y ' + y)
        y_label.setPos(bx, (az + bz) / 2)
        for item in items:
            item.show()
        # Zero-length legs have no distinct side on which to place a label.
        x_label.setVisible(abs(bx - ax) > 1e-12)
        y_label.setVisible(abs(bz - az) > 1e-12)

    def _move_endpoint(self, row, endpoint, target):
        if self._moving_endpoint or row >= len(self.measurements) or row >= len(self.graphics):
            return
        m = self.measurements[row]
        xf, zf = self._plot_factors()
        x, z = target.pos().x() / xf, target.pos().y() / zf
        index = m.get('source_index', -1)
        series = self.series[index] if 0 <= index < len(self.series) else None
        snapped = m.get('snapped', False)
        if snapped:
            z = self._snap(series, x) if series else None
        key = 'a' if endpoint == 0 else 'b'
        if not is_finite(x) or not is_finite(z):
            x, z = m[key]  # Keep the last valid endpoint at gaps / outside coverage.
        self._moving_endpoint = True
        try:
            target.setPos(x * xf, z * zf)
            m[key] = (x, z)
            m['metrics'] = self._metrics(m['a'], m['b'], series, snapped)
            triangle, _ = self.graphics[row]
            self._update_triangle(triangle, m['a'], m['b'], m['metrics'], row + 1)
            for col, value in enumerate(self.row_text(m['metrics'])):
                cell = self.table.item(row, col)
                if cell is not None:
                    cell.setText(value)
        finally:
            self._moving_endpoint = False
        self.measurementsChanged.emit()
