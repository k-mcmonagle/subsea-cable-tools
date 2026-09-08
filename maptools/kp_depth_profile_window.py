"""Live/frozen depth profiles with repeatable measurements and snapshot exports."""
from __future__ import annotations
import math

import pyqtgraph as pg
from qgis.PyQt.QtCore import QSettings, QTimer, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
    QVBoxLayout, QPushButton, QComboBox, QDoubleSpinBox, QTableWidget,
    QTableWidgetItem, QFileDialog, QMessageBox, QWidget, QAbstractItemView)
from ..qgis_compat import WINDOW_HINT_CLOSE, WINDOW_HINT_TITLE, WINDOW_TYPE_TOOL
from ..slope_utils import interpolate_covered, is_finite
from .kp_profile_math import merged_contour_crossings, profile_slope_series
from .profile_measurements import UNITS, measurement, write_profile_csv

_COLORS = ['#1565c0', '#c05a10', '#238443', '#8e44ad', '#b22222']


class ProfileAxis(pg.AxisItem):
    """KP labels retain physical profile-distance spacing and measurements."""
    def __init__(self, window):
        self.window = window
        super().__init__(orientation='bottom')

    def tickStrings(self, values, scale, spacing):
        window = self.window
        if window.kp_check.isChecked():
            kps = [window._kp_at_distance(v / UNITS[window.x_units.currentText()]) for v in values]
            return ['' if kp is None else '%.4f' % kp for kp in kps]
        return super().tickStrings(values, scale, spacing)


class KPDepthProfileWindow(QDialog):
    frozenChanged = pyqtSignal(bool)

    def __init__(self, parent=None, unit='m'):
        super().__init__(parent)
        self.setWindowTitle('Range Line Depth Profile')
        self.setWindowFlags(WINDOW_TYPE_TOOL | WINDOW_HINT_CLOSE | WINDOW_HINT_TITLE)
        self.resize(900, 700)
        self.user_closed = False
        self.frozen = False
        self._pending = self._sampler = self._distance_area = None
        self._profile = None
        self._route_frame = None
        self._preview = None
        self._moving_endpoint = False
        self._measurement_graphics = []
        self._measurements = []
        self._first_point = None
        self._series = []
        self._settings = QSettings('SubseaCableTools', 'KPMouseTool')
        self._timer = QTimer(self); self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        layout = QVBoxLayout(self)
        row = QHBoxLayout(); layout.addLayout(row)
        self.freeze_btn = QPushButton('Freeze (Space)'); self.freeze_btn.setCheckable(True)
        self.freeze_btn.setToolTip('Space locks the map line and profile. Resume clears measurements and follows the pointer again.')
        self.freeze_btn.toggled.connect(self.set_frozen); row.addWidget(self.freeze_btn)
        self.source_mode = QComboBox(); self.source_mode.addItems(['Auto / native rasters', 'Contours only', 'Rasters only'])
        self.source_mode.currentIndexChanged.connect(self._calculation_changed); row.addWidget(self.source_mode)
        self.raw_check = QCheckBox('Raw cells'); self.raw_check.toggled.connect(self._calculation_changed); row.addWidget(self.raw_check)
        row.addWidget(QLabel('Slope length:'))
        self.window_spin = QDoubleSpinBox(); self.window_spin.setRange(0, 100000); self.window_spin.setDecimals(2)
        self.window_spin.setSuffix(' m'); self.window_spin.setSpecialValueText('Automatic')
        self.window_spin.valueChanged.connect(self._calculation_changed); row.addWidget(self.window_spin)
        row = QHBoxLayout(); layout.addLayout(row)
        self.x_units = QComboBox(); self.x_units.addItems(list(UNITS)); self.x_units.setCurrentText('m')
        self.z_units = QComboBox(); self.z_units.addItems(['m', 'ft']); row.addWidget(QLabel('Horizontal:')); row.addWidget(self.x_units)
        self.kp_check = QCheckBox('Route KP labels'); self.kp_check.setEnabled(False)
        self.kp_check.setToolTip('Nearest route KP in km at each tick. Spacing and X measurements remain distance along the range line; cross-route lines can repeat KPs.')
        row.addWidget(self.kp_check)
        row.addWidget(QLabel('Vertical:')); row.addWidget(self.z_units)
        self.invert_check = QCheckBox('Deeper downward'); self.invert_check.setChecked(True)
        row.addWidget(self.invert_check)
        self.shade_check = QCheckBox('Shade seabed / slope'); self.shade_check.setChecked(True); row.addWidget(self.shade_check)
        self.pin_check = QCheckBox('Keep on map click'); row.addWidget(self.pin_check)
        self.scale_check = QCheckBox('True scale 1:1'); self.scale_check.setChecked(True)
        self.scale_check.setToolTip('Equal physical horizontal and vertical scales, including mixed units. Uncheck for independent axes / vertical exaggeration.')
        row.addWidget(self.scale_check)
        self.plot_container = QWidget(); plots = QVBoxLayout(self.plot_container); plots.setContentsMargins(0,0,0,0)
        layout.addWidget(self.plot_container, 1)
        self.depth_widget = pg.PlotWidget(background='w', axisItems={'bottom': ProfileAxis(self)}); self.slope_widget = pg.PlotWidget(background='w', axisItems={'bottom': ProfileAxis(self)})
        plots.addWidget(self.depth_widget, 3); plots.addWidget(self.slope_widget, 2)
        self.depth_item = self.depth_widget.getPlotItem(); self.slope_item = self.slope_widget.getPlotItem()
        self.slope_widget.setXLink(self.depth_widget)
        for item in (self.depth_item, self.slope_item):
            item.showGrid(x=True, y=True, alpha=.2)
            item.getAxis('bottom').enableAutoSIPrefix(False)
            item.getAxis('left').enableAutoSIPrefix(False)
        self._legend = self.depth_item.addLegend()
        self.depth_widget.scene().sigMouseClicked.connect(self._plot_click)
        self.depth_widget.scene().sigMouseMoved.connect(self._plot_move)
        row = QHBoxLayout(); layout.addLayout(row)
        menu = self.depth_item.vb.getMenu(None)
        self.measure_action = menu.addAction('Measure')
        self.measure_action.setCheckable(True)
        self.measure_action.setToolTip('Click two endpoints; drag to pan and scroll to zoom. Uncheck to stop measuring.')
        self.measure_action.toggled.connect(self._measure_mode)
        row.addWidget(QLabel('Right-click plot to measure'))
        self.snap_check = QCheckBox('Snap to profile'); self.snap_check.setChecked(True); row.addWidget(self.snap_check)
        self.measure_source = QComboBox(); row.addWidget(self.measure_source, 1)
        self.measure_source.currentIndexChanged.connect(self._cancel_pending_measurement)
        self.snap_check.toggled.connect(self._cancel_pending_measurement)
        self.undo_btn = QPushButton('Delete / undo'); self.undo_btn.clicked.connect(self._delete_measurement); row.addWidget(self.undo_btn)
        clear = QPushButton('Clear measurements'); clear.clicked.connect(self.clear_measurements); row.addWidget(clear)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['Length', 'X', 'Y', 'Angle (°)'])
        self.table.setToolTip('Length: straight line between endpoints (horizontal units). X: horizontal separation. Y: absolute depth difference (vertical units). Angle: unsigned endpoint inclination to horizontal (0–90°), not the maximum seabed slope between points. Measurements use true distances, regardless of vertical exaggeration or KP labels.')
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(145); layout.addWidget(self.table)
        row = QHBoxLayout(); layout.addLayout(row)
        self.status_label = QLabel('Move over the map; Space freezes the line.'); self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)
        png = QPushButton('PNG…'); png.clicked.connect(self.export_png); row.addWidget(png)
        csv = QPushButton('CSV…'); csv.clicked.connect(self.export_csv); row.addWidget(csv)
        close = QPushButton('Close'); close.clicked.connect(self.close); row.addWidget(close)
        for signal in (self.x_units.currentTextChanged, self.z_units.currentTextChanged,
                       self.invert_check.toggled, self.shade_check.toggled, self.kp_check.toggled, self.scale_check.toggled):
            signal.connect(self._redraw)
        from qgis.PyQt.QtGui import QKeySequence
        try:
            from qgis.PyQt.QtGui import QShortcut
        except ImportError:
            from qgis.PyQt.QtWidgets import QShortcut
        self._freeze_shortcut = QShortcut(QKeySequence('Space'), self)
        self._freeze_shortcut.setAutoRepeat(False)
        self._freeze_shortcut.activated.connect(lambda: self.set_frozen(not self.frozen))
        self._working_profile = None
        self._slopes_x, self._slopes = [], []

    def configure(self, sampler, distance_area, route_frame=None):
        self._sampler, self._distance_area = sampler, distance_area
        self._route_frame = route_frame
        self.kp_check.setEnabled(route_frame is not None)

    def _kp_at_distance(self, distance_m):
        profile = self._profile or {}
        length = profile.get('length_m', 0)
        endpoints = profile.get('endpoints')
        if self._route_frame is None or not endpoints or length <= 0 or not 0 <= distance_m <= length:
            return None
        from qgis.core import QgsPointXY
        a,b = endpoints; t = distance_m / length
        hit = self._route_frame.kp_at_point(QgsPointXY(a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])))
        return hit.kp_km if hit.snapped_xy is not None else None

    def schedule(self, origin, target):
        if self.frozen or self.user_closed:
            return
        self._pending = (origin, target)
        if not self._timer.isActive():
            self._timer.start(150)

    def set_frozen(self, frozen):
        frozen = bool(frozen)
        if frozen and not self.frozen:
            # Flush the last map line before locking both plot and overlay.
            self._timer.stop()
            self._refresh()
        self.frozen = frozen
        self.freeze_btn.blockSignals(True); self.freeze_btn.setChecked(frozen); self.freeze_btn.blockSignals(False)
        self.freeze_btn.setText('Resume (Space)' if frozen else 'Freeze (Space)')
        if not frozen:
            self.clear_measurements()
            self.measure_action.setChecked(False)
        self.frozenChanged.emit(frozen)
        self._update_status()

    def _refresh(self):
        if self._pending is None or self._sampler is None or not self.isVisible():
            return
        origin, target = self._pending; self._pending = None
        try:
            self._profile = self._sampler.profile(origin, target, self._distance_area)
            self._profile['endpoints'] = [(origin.x(), origin.y()), (target.x(), target.y())]
            self._measurements = []; self._first_point = None; self.table.setRowCount(0)
            self._redraw()
        except Exception as exc:
            self._profile = None
            self._redraw()
            self.status_label.setText('Depth sampling failed: ' + str(exc))

    def _calculation_changed(self, *_):
        # Existing measurements belong to the previous calculation surface.
        self._measurements = []; self._first_point = None; self.table.setRowCount(0)
        self._redraw()

    def _redraw(self, *_):
        self.depth_item.clear(); self.slope_item.clear(); self._legend.clear()
        self._preview = None
        self._measurement_graphics = []
        for item in (self.depth_item, self.slope_item):
            axis = item.getAxis("bottom"); axis.picture = None; axis.update()
        self._series = []
        xf, zf = UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]
        self.depth_item.setLabel('left', 'Depth, positive down', units=self.z_units.currentText())
        self.depth_item.vb.invertY(self.invert_check.isChecked())
        self.depth_item.vb.setAspectLocked(self.scale_check.isChecked(), ratio=zf/xf)
        self.slope_item.setLabel('left', 'Slope (+ = shoaling)', units='°')
        self.slope_item.setLabel('bottom', 'Nearest route KP (km) — spacing along range line' if self.kp_check.isChecked() else 'Distance from origin', units=None if self.kp_check.isChecked() else self.x_units.currentText())
        if not self._profile:
            return
        profile = dict(self._profile)
        profile['rasters'] = [dict(s) for s in profile.get('rasters', [])]
        profile['contours'] = list(profile.get('contours', []))
        mode = self.source_mode.currentIndex()
        if mode == 1: profile['rasters'] = []
        if mode == 2: profile['contours'] = []
        profile['source_mode'] = self.source_mode.currentText()
        profile['slope_window_m'] = self.window_spin.value()
        for series in profile['rasters']:
            if self.raw_check.isChecked():
                series['y'] = series.get('raw_y', series['y'])
                series['sampling'] = 'nearest'
        self._series.extend(profile['rasters'])
        if profile['contours']:
            x, y = merged_contour_crossings(profile)
            self._series.append({'name':'Contours (linear crossings)', 'x':x, 'y':y})
        previous = self.measure_source.currentText()
        self.measure_source.blockSignals(True); self.measure_source.clear()
        for series in self._series: self.measure_source.addItem(series['name'])
        if previous: self.measure_source.setCurrentText(previous)
        self.measure_source.blockSignals(False)
        for i, series in enumerate(self._series):
            x = [v * xf for v in series['x']]
            y = [v * zf if is_finite(v) else math.nan for v in series['y']]
            finite = [v for v in y if math.isfinite(v)]
            kwargs = {}
            if finite and self.shade_check.isChecked():
                kwargs = {'fillLevel': max(finite) + max((max(finite)-min(finite))*.05, .1*zf),
                          'brush': pg.mkBrush(80, 110, 130, 35)}
            self.depth_item.plot(x, y, name=series['name'], pen=pg.mkPen(_COLORS[i % len(_COLORS)], width=2),
                                 connect='finite', antialias=True, **kwargs)
        self._slopes_x, self._slopes, _ = profile_slope_series(profile, positive_down=True)
        self._working_profile = profile
        sx = [v * xf for v in self._slopes_x]
        sy = [v if is_finite(v) else math.nan for v in self._slopes]
        kwargs = {'fillLevel':0, 'brush':pg.mkBrush(90, 100, 120, 40)} if self.shade_check.isChecked() else {}
        self.slope_item.plot(sx, sy, pen=pg.mkPen('#444444', width=2), connect='finite', antialias=True, **kwargs)
        self._draw_measurements()
        self._update_status()

    def _measure_mode(self, checked):
        if checked:
            self.set_frozen(True)
        self._first_point = None
        self.depth_item.vb.setMouseEnabled(x=True, y=True)
        self._redraw()

    def _plot_click(self, event):
        if not self.measure_action.isChecked() or event.button() != Qt.MouseButton.LeftButton:
            return
        # Pyqtgraph emits clicks separately from drags. Ignore clicks already
        # consumed by plot controls and double-clicks used for navigation.
        if getattr(event, 'isAccepted', lambda: False)() or getattr(event, 'double', lambda: False)():
            return
        if not self.depth_item.vb.sceneBoundingRect().contains(event.scenePos()):
            return
        index = self.measure_source.currentIndex()
        if not 0 <= index < len(self._series): return
        point = self.depth_item.vb.mapSceneToView(event.scenePos())
        x = point.x() / UNITS[self.x_units.currentText()]
        z = point.y() / UNITS[self.z_units.currentText()]
        series = self._series[index]
        if self.snap_check.isChecked():
            z = interpolate_covered(series['x'], series['y'], x)
            if z is None:
                self.status_label.setText('No supported profile at that position. Choose a point within coverage.')
                return
        if not is_finite(x) or not is_finite(z): return
        event.accept()
        if self._first_point is None:
            self._first_point = ((x, z), index, self.snap_check.isChecked())
            self.status_label.setText('First point placed. Click the second point; Escape cancels this measurement.')
            self._redraw()
            self.status_label.setText("First point placed. Click the second point; Escape cancels.")
            return
        a, first_index, snapped = self._first_point
        if first_index != index or snapped != self.snap_check.isChecked():
            self._first_point = None
            self.status_label.setText('Source or snapping changed. Place the first point again.')
            return
        metrics = measurement(a, (x, z), series['x'] if snapped else None, series['y'] if snapped else None)
        self._measurements.append({'a':a, 'b':(x,z), 'source':series['name'], 'source_index':index, 'snapped':snapped, 'metrics':metrics})
        self._first_point = None
        self._redraw()

    def _cancel_pending_measurement(self, *_):
        if self._first_point is not None:
            self._first_point = None
            self._redraw()

    def _measurement_text(self, values):
        xf, zf = UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]
        return ['%.3f %s' % (values[key]*factor, unit) for key,factor,unit in
                [('endpoint_distance_m',xf,self.x_units.currentText()),
                 ('width_m',xf,self.x_units.currentText()),
                 ('height_m',zf,self.z_units.currentText())]]

    def _plot_move(self, position):
        if not self._first_point or not self.measure_action.isChecked():
            return
        a,index,snapped = self._first_point
        if (not self.depth_item.vb.sceneBoundingRect().contains(position)
                or index != self.measure_source.currentIndex()
                or snapped != self.snap_check.isChecked()):
            if self._preview:
                for item in self._preview: item.hide()
            return
        point = self.depth_item.vb.mapSceneToView(position)
        xf, zf = UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]
        x,z = point.x()/xf, point.y()/zf
        series = self._series[index]
        if snapped: z = interpolate_covered(series['x'],series['y'],x)
        if not is_finite(x) or not is_finite(z):
            if self._preview:
                for item in self._preview: item.hide()
            return
        if self._preview is None:
            self._preview = self._make_triangle(preview=True)
        self._update_triangle(self._preview, a, (x,z), measurement(a,(x,z)))

    def _make_triangle(self, preview=False):
        diagonal = pg.PlotDataItem(pen=pg.mkPen('#c2185b',width=2,
            style=Qt.PenStyle.DashLine if preview else Qt.PenStyle.SolidLine))
        legs = pg.PlotDataItem(pen=pg.mkPen('#c2185b',width=1,style=Qt.PenStyle.DashLine))
        labels = [pg.TextItem(color='#8e1243',anchor=anchor,
                  fill=pg.mkBrush(255,255,255,210)) for anchor in ((.5,1),(.5,0),(0,.5))]
        items = (diagonal,legs,*labels)
        for item in items: self.depth_item.addItem(item,ignoreBounds=True)
        return items

    @staticmethod
    def _angle_text(values):
        angle = values.get('angle_deg')
        return '—' if angle is None else '%.2f°' % angle

    def _update_triangle(self, items, a, b, values, number=None):
        xf, zf = UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]
        ax,az,bx,bz = a[0]*xf,a[1]*zf,b[0]*xf,b[1]*zf
        diagonal,legs,length_label,x_label,y_label = items
        diagonal.setData([ax,bx],[az,bz])
        # Right-angle corner is (b.x, a.z); legs retain physical X/Y meaning.
        legs.setData([ax,bx,bx],[az,az,bz])
        length,x,y = self._measurement_text(values)
        prefix = '%d: ' % number if number is not None else ''
        # Put labels outside the triangle so shallow slopes do not stack X
        # and diagonal text on top of one another. Respect depth-axis inversion.
        down_on_screen = (bz-az) * (1 if self.invert_check.isChecked() else -1) >= 0
        x_label.setAnchor((.5,1 if down_on_screen else 0))
        length_label.setAnchor((.5,0 if down_on_screen else 1))
        y_label.setAnchor((0 if bx >= ax else 1,.5))
        length_label.setText(prefix+'Length '+length+' / '+self._angle_text(values))
        length_label.setPos((ax+bx)/2,(az+bz)/2)
        x_label.setText('X '+x); x_label.setPos((ax+bx)/2,az)
        y_label.setText('Y '+y); y_label.setPos(bx,(az+bz)/2)
        # Zero-length legs have no distinct side on which to place a label.
        for item in items: item.show()
        x_label.setVisible(abs(bx-ax)>1e-12)
        y_label.setVisible(abs(bz-az)>1e-12)

    def _draw_measurements(self):
        xf, zf = UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]
        self.table.setRowCount(len(self._measurements))
        for row, m in enumerate(self._measurements):
            a,b = m['a'],m['b']
            triangle = self._make_triangle()
            handles = []
            for endpoint, point in enumerate((a,b)):
                handle = pg.TargetItem(pos=(point[0]*xf,point[1]*zf),size=11,symbol='o',
                    movable=True,pen=pg.mkPen('#c2185b'),brush=pg.mkBrush('#ffffff'),
                    hoverPen=pg.mkPen('#1565c0',width=2))
                handle.setZValue(100)
                self.depth_item.addItem(handle,ignoreBounds=True)
                handle.sigPositionChanged.connect(lambda target,r=row,e=endpoint: self._move_endpoint(r,e,target))
                handles.append(handle)
            self._measurement_graphics.append((triangle,handles))
            cells = self._measurement_text(m['metrics'])
            self._update_triangle(triangle,a,b,m['metrics'],row+1)
            cells.append(self._angle_text(m['metrics']))
            for col,value in enumerate(cells):
                cell = QTableWidgetItem(value); cell.setToolTip(m['source']); self.table.setItem(row,col,cell)
        if self._first_point:
            (x,z),_,_ = self._first_point
            self.depth_item.plot([x*xf],[z*zf],pen=None,symbol='o',symbolBrush='#c2185b')

    def _move_endpoint(self, row, endpoint, target):
        if self._moving_endpoint or row >= len(self._measurements):
            return
        m = self._measurements[row]
        xf,zf = UNITS[self.x_units.currentText()],UNITS[self.z_units.currentText()]
        x,z = target.pos().x()/xf,target.pos().y()/zf
        index = m.get('source_index', -1)
        series = self._series[index] if 0 <= index < len(self._series) else None
        snapped = m.get('snapped',False)
        if snapped:
            z = interpolate_covered(series['x'],series['y'],x) if series else None
        key = 'a' if endpoint == 0 else 'b'
        if not is_finite(x) or not is_finite(z):
            x,z = m[key]  # Keep the last valid endpoint at gaps / outside coverage.
        self._moving_endpoint = True
        try:
            target.setPos(x*xf,z*zf)
            m[key] = (x,z)
            m['metrics'] = measurement(m['a'],m['b'],
                series['x'] if snapped and series else None,
                series['y'] if snapped and series else None)
            triangle,_ = self._measurement_graphics[row]
            self._update_triangle(triangle,m['a'],m['b'],m['metrics'],row+1)
            cells = self._measurement_text(m['metrics'])+[self._angle_text(m['metrics'])]
            for col,value in enumerate(cells): self.table.item(row,col).setText(value)
        finally:
            self._moving_endpoint = False

    def _delete_measurement(self):
        if self._first_point:
            self._first_point = None
        elif self._measurements:
            row = self.table.currentRow()
            self._measurements.pop(row if row >= 0 else -1)
        self._redraw()

    def clear_measurements(self):
        self._measurements = []; self._first_point = None; self.table.setRowCount(0)
        if self._profile: self._redraw()

    def _update_status(self):
        state = 'Frozen' if self.frozen else 'Live'
        valid = [abs(v) for v in self._slopes if is_finite(v)]
        text = '%s — max supported slope %.2f°' % (state, max(valid)) if valid else state + ' — no supported slope baseline'
        widths = [w for w in (self._working_profile or {}).get('slope_baseline_m', []) if w]
        if widths:
            text += ' (baseline %.2f–%.2f m)' % (min(widths),max(widths))
        terrace = (self._working_profile or {}).get('terrace_baseline_m', 0)
        if terrace:
            text = text.replace('max supported slope', 'max averaged slope')
            text += '. Repeated raster terraces: suggested averaging length %.2f m; native resolution needs verification' % terrace
            if self.window_spin.value() and self.window_spin.value() < terrace:
                text += ' — selected length may exaggerate step edges'
        if self.measure_action.isChecked(): text += '. Click two points; drag to pan; scroll to zoom. Right-click → Measure to stop; Escape cancels; Delete removes a measurement.'
        self.status_label.setText(text)

    def _export(self, kind):
        self.set_frozen(True)
        if not self._profile: return
        path, _ = QFileDialog.getSaveFileName(self, 'Export frozen profile', 'profile.'+kind,
                                            'PNG image (*.png)' if kind=='png' else 'CSV table (*.csv)')
        if not path: return
        if not path.lower().endswith('.'+kind): path += '.'+kind
        try:
            if kind == 'png':
                if not self.plot_container.grab().save(path, 'PNG'): raise OSError('Could not write image')
            else:
                distances = {x for s in self._series for x in s['x']} | set(self._slopes_x)
                self._working_profile['route_kp'] = {x:self._kp_at_distance(x) for x in distances}
                self._working_profile['axis_labels'] = 'nearest route KP (km); distance spacing' if self.kp_check.isChecked() else 'distance'
                write_profile_csv(path,self._working_profile,self._series,self._slopes_x,self._slopes,self._measurements)
        except Exception as exc:
            QMessageBox.warning(self,'Export failed',str(exc))

    def export_png(self): self._export('png')
    def export_csv(self): self._export('csv')

    def keyPressEvent(self,event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.set_frozen(not self.frozen); event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self._delete_measurement(); event.accept()
        elif event.key() == Qt.Key.Key_Escape and self._first_point:
            self._first_point = None; self._redraw(); event.accept()
        elif event.key() == Qt.Key.Key_Escape and self.measure_action.isChecked():
            self.measure_action.setChecked(False); event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.close(); event.accept()
        else: super().keyPressEvent(event)

    def clear_profile(self):
        self._timer.stop(); self._pending = None; self._profile = None
        self._working_profile = None; self._slopes_x = []; self._slopes = []
        self.clear_measurements(); self.set_frozen(False); self._redraw()

    def showEvent(self,event):
        geometry = self._settings.value('profileWindowGeometry')
        if geometry is not None: self.restoreGeometry(geometry)
        super().showEvent(event)

    def hideEvent(self,event):
        self._timer.stop(); self._pending = None
        self._settings.setValue('profileWindowGeometry',self.saveGeometry())
        super().hideEvent(event)

    def closeEvent(self,event):
        self.user_closed = True
        self._timer.stop(); self._pending = None
        super().closeEvent(event)

    def cleanup(self):
        self._timer.stop(); self._pending = None; self._sampler = None
        self.close(); self.deleteLater()
