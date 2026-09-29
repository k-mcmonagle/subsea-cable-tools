"""Live/frozen depth profiles with repeatable measurements and snapshot exports."""
from __future__ import annotations
import logging
import math

import pyqtgraph as pg
from qgis.PyQt.QtCore import QEvent, QSettings, QTimer, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
    QVBoxLayout, QPushButton, QComboBox, QDoubleSpinBox, QFileDialog, QMessageBox, QWidget)
from ..kp_axis import KPCrossings, format_kp
from ..plugin_log import log_exception
from ..kp_axis_item import KPAxisItem
from ..qgis_compat import WINDOW_HINT_CLOSE, WINDOW_HINT_TITLE, WINDOW_TYPE_TOOL
from ..slope_utils import interpolate_covered, is_finite
from .kp_profile_math import merged_contour_crossings, profile_slope_series
from .profile_measure_controller import HINT, ProfileMeasureController
from .profile_measurements import UNITS, write_profile_csv

_COLORS = ['#1565c0', '#c05a10', '#238443', '#8e44ad', '#b22222']
# Nearest-KP samples along the range line; tick positions are then polished
# against the route itself, so labels are exact rather than interpolated.
_KP_SAMPLES = 160
_CURSOR_PEN = pg.mkPen('#d81b60', width=1, style=Qt.PenStyle.DashLine)
_CURSOR_HINT = 'Hover the plot to mark the position on the map.'


class KPDepthProfileWindow(QDialog):
    frozenChanged = pyqtSignal(bool)
    # Plot cursor position on the range line (project-CRS QgsPointXY), or
    # None when the cursor leaves the plots. The map tool mirrors it with a
    # marker so plot and map stay aligned.
    cursorMoved = pyqtSignal(object)

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
        self._kp_crossings = None
        self._cursor_m = None
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
        self.kp_check.setChecked(self._settings.value('profileRouteKpLabels', False, type=bool))
        self.kp_check.setToolTip("Ticks at round route KPs (…0.100, 0.250, 0.500, 1.000…) placed where the range "
                                 "line's nearest route KP crosses them, labelled to 3 dp. Spacing and X measurements "
                                 "remain distance along the range line; a line across the route can revisit KPs.")
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
        self.cursor_label = QLabel(_CURSOR_HINT)
        self.cursor_label.setStyleSheet('color: #555;')
        layout.addWidget(self.cursor_label)
        self.depth_axis, self.slope_axis = KPAxisItem(), KPAxisItem()
        self.depth_widget = pg.PlotWidget(background='w', axisItems={'bottom': self.depth_axis})
        self.slope_widget = pg.PlotWidget(background='w', axisItems={'bottom': self.slope_axis})
        plots.addWidget(self.depth_widget, 3); plots.addWidget(self.slope_widget, 2)
        self.depth_item = self.depth_widget.getPlotItem(); self.slope_item = self.slope_widget.getPlotItem()
        self.slope_widget.setXLink(self.depth_widget)
        for item in (self.depth_item, self.slope_item):
            item.showGrid(x=True, y=True, alpha=.2)
            item.getAxis('bottom').enableAutoSIPrefix(False)
            item.getAxis('left').enableAutoSIPrefix(False)
        self._legend = self.depth_item.addLegend()
        # Synchronised cursor: one dashed line per plot, mirrored on the map.
        self._cursor_lines = []
        for _item in (self.depth_item, self.slope_item):
            line = pg.InfiniteLine(angle=90, movable=False, pen=_CURSOR_PEN)
            line.setZValue(50); line.hide()
            self._cursor_lines.append(line)
        for widget in (self.depth_widget, self.slope_widget):
            widget.scene().sigMouseMoved.connect(self._on_plot_hover)
            widget.installEventFilter(self)
        self.measure = ProfileMeasureController(
            self, plot_factors=lambda: (UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]),
            text_units=lambda: (self.x_units.currentText(), self.z_units.currentText()),
            depth_down=self.invert_check.isChecked)
        self.measure.modeChanged.connect(self._measure_mode)
        self.measure.statusChanged.connect(lambda text: self.status_label.setText(text))
        self.measure.attach(self.depth_item)
        self.measure_action = self.measure.action
        self.snap_check = self.measure.snap_check
        self.measure_source = self.measure.source_combo
        self.undo_btn = self.measure.delete_btn
        self.table = self.measure.table
        row = QHBoxLayout(); layout.addLayout(row)
        row.addWidget(QLabel('Right-click plot to measure'))
        row.addWidget(self.snap_check)
        row.addWidget(self.measure_source, 1)
        row.addWidget(self.undo_btn)
        row.addWidget(self.measure.clear_btn)
        layout.addWidget(self.table)
        row = QHBoxLayout(); layout.addLayout(row)
        self.status_label = QLabel('Move over the map; Space freezes the line.'); self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)
        png = QPushButton('PNG…'); png.clicked.connect(self.export_png); row.addWidget(png)
        csv = QPushButton('CSV…'); csv.clicked.connect(self.export_csv); row.addWidget(csv)
        close = QPushButton('Close'); close.clicked.connect(self.close); row.addWidget(close)
        for signal in (self.x_units.currentTextChanged, self.z_units.currentTextChanged,
                       self.invert_check.toggled, self.shade_check.toggled, self.kp_check.toggled, self.scale_check.toggled):
            signal.connect(self._redraw)
        self.kp_check.toggled.connect(lambda on: self._settings.setValue('profileRouteKpLabels', bool(on)))
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

    def point_at_distance(self, distance_m):
        """Project-CRS point ``distance_m`` along the current range line.

        Same linear parameterisation the depth sampler uses, so the map
        marker sits exactly on the sampled station.
        """
        profile = self._profile or {}
        length = profile.get('length_m', 0)
        endpoints = profile.get('endpoints')
        if not endpoints or length <= 0 or not 0 <= distance_m <= length:
            return None
        from qgis.core import QgsPointXY
        a, b = endpoints; t = distance_m / length
        return QgsPointXY(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))

    def _kp_at_distance(self, distance_m):
        if self._route_frame is None:
            return None
        point = self.point_at_distance(distance_m)
        if point is None:
            return None
        hit = self._route_frame.kp_at_point(point)
        return hit.kp_km if hit.snapped_xy is not None else None

    def _crossings(self):
        """Nearest-KP samples along the range line (built on first use)."""
        if self._kp_crossings is None and self._route_frame is not None and self._profile:
            length = self._profile.get('length_m', 0)
            if length > 0:
                xs = [length * i / _KP_SAMPLES for i in range(_KP_SAMPLES + 1)]
                self._kp_crossings = KPCrossings(xs, [self._kp_at_distance(x) for x in xs],
                                                 refine=self._kp_at_distance)
        return self._kp_crossings

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
            self._kp_crossings = None
            self.measure.reset()
            self._redraw()
        except Exception as exc:  # noqa: BLE001 - shown in the window; traceback in the debug log
            log_exception('KP depth profile: sampling failed', level=logging.DEBUG)
            self._profile = None
            self._kp_crossings = None
            self._redraw()
            self.status_label.setText('Depth sampling failed: ' + str(exc))

    def _calculation_changed(self, *_):
        # Existing measurements belong to the previous calculation surface.
        self.measure.reset()
        self._redraw()

    def _redraw(self, *_):
        self.depth_item.clear(); self.slope_item.clear(); self._legend.clear()
        for item, line in zip((self.depth_item, self.slope_item), self._cursor_lines):
            item.addItem(line, ignoreBounds=True)
        self._series = []
        xf, zf = UNITS[self.x_units.currentText()], UNITS[self.z_units.currentText()]
        crossings = self._crossings() if self.kp_check.isChecked() else None
        for axis in (self.depth_axis, self.slope_axis):
            if crossings:
                axis.set_mapped(crossings, metres_per_unit=1.0 / xf)
            else:
                axis.set_plain()
        self.depth_item.setLabel('left', 'Depth, positive down', units=self.z_units.currentText())
        self.depth_item.vb.invertY(self.invert_check.isChecked())
        self.depth_item.vb.setAspectLocked(self.scale_check.isChecked(), ratio=zf/xf)
        self.slope_item.setLabel('left', 'Slope (+ = shoaling)', units='°')
        self.slope_item.setLabel('bottom', 'Nearest route KP (km) — spacing along range line' if self.kp_check.isChecked() else 'Distance from origin', units=None if self.kp_check.isChecked() else self.x_units.currentText())
        if not self._profile:
            self.hide_cursor()
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
        self.measure.set_series(self._series)
        if self._cursor_m is not None:
            # Units or data changed under a parked cursor: redraw it in place.
            self.show_cursor_at_distance(self._cursor_m, emit=False)
        self._update_status()

    # ------------------------------------------------------------ cursor sync
    def _on_plot_hover(self, scene_pos):
        for item in (self.depth_item, self.slope_item):
            if item.vb.sceneBoundingRect().contains(scene_pos):
                x = item.vb.mapSceneToView(scene_pos).x()
                self.show_cursor_at_distance(x / UNITS[self.x_units.currentText()])
                return
        self.hide_cursor()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Leave and obj in (self.depth_widget, self.slope_widget):
            self.hide_cursor()
        return super().eventFilter(obj, event)

    def show_cursor_at_distance(self, distance_m, emit=True):
        """Show the plot cursor ``distance_m`` along the range line.

        Called on plot hover (``emit`` → the map marker follows) and by the
        map tool while the pointer tracks a frozen range line on the map.
        """
        length = (self._profile or {}).get('length_m', 0)
        if not self._profile or length <= 0 or distance_m is None \
                or not -1e-9 <= distance_m <= length + 1e-9:
            self.hide_cursor(emit)
            return
        distance_m = min(max(distance_m, 0.0), length)
        self._cursor_m = distance_m
        x = distance_m * UNITS[self.x_units.currentText()]
        for line in self._cursor_lines:
            line.setValue(x); line.show()
        self.cursor_label.setText(self._cursor_text(distance_m))
        if emit:
            self.cursorMoved.emit(self.point_at_distance(distance_m))

    def hide_cursor(self, emit=True):
        was_shown = self._cursor_m is not None
        self._cursor_m = None
        for line in self._cursor_lines:
            line.hide()
        self.cursor_label.setText(_CURSOR_HINT)
        if emit and was_shown:
            self.cursorMoved.emit(None)

    def _cursor_text(self, distance_m):
        xu, zu = self.x_units.currentText(), self.z_units.currentText()
        xf, zf = UNITS[xu], UNITS[zu]
        parts = ['Range %.2f %s' % (distance_m * xf, xu)]
        kp = self._kp_at_distance(distance_m)
        if kp is not None:
            parts.append('KP ' + format_kp(kp))
        for series in self._series:
            z = interpolate_covered(series['x'], series['y'], distance_m)
            if is_finite(z):
                name = series['name'] if len(self._series) > 1 else 'Depth'
                parts.append('%s %.2f %s' % (name, z * zf, zu))
        if self._slopes_x:
            slope = interpolate_covered(self._slopes_x, self._slopes, distance_m)
            if is_finite(slope):
                parts.append('Slope %.2f°' % slope)
        return '  |  '.join(parts)

    def _measure_mode(self, checked):
        if checked:
            self.set_frozen(True)
        self._update_status()

    def _delete_measurement(self):
        self.measure.delete_measurement()

    def clear_measurements(self):
        self.measure.clear()

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
        if self.measure.active: text += '. ' + HINT + ' Right-click → Measure to stop.'
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
                write_profile_csv(path,self._working_profile,self._series,self._slopes_x,self._slopes,self.measure.measurements)
        except Exception as exc:
            QMessageBox.warning(self,'Export failed',str(exc))

    def export_png(self): self._export('png')
    def export_csv(self): self._export('csv')

    def keyPressEvent(self,event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.set_frozen(not self.frozen); event.accept()
        elif self.measure.handle_key(event):
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.close(); event.accept()
        else: super().keyPressEvent(event)

    def clear_profile(self):
        self.hide_cursor()
        self._timer.stop(); self._pending = None; self._profile = None; self._kp_crossings = None
        self._working_profile = None; self._slopes_x = []; self._slopes = []
        self.clear_measurements(); self.set_frozen(False); self._redraw()

    def showEvent(self,event):
        geometry = self._settings.value('profileWindowGeometry')
        if geometry is not None: self.restoreGeometry(geometry)
        super().showEvent(event)

    def hideEvent(self,event):
        self.hide_cursor()
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
