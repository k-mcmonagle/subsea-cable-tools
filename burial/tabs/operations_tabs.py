# -*- coding: utf-8 -*-
"""Acquired data, interactive assessment and reporting workflow tabs."""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from qgis.core import QgsProject, QgsVectorLayer, QgsVectorLayerFeatureSource
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...qgis_compat import QSvgWidget
from ...laydata import burial_data as data, burial_report as report
from .. import operations
from ..operations_dialogs import JsonDialog, MappingDialog, ProcessingDialog, buttons, combo, spin

_USER = getattr(Qt, 'ItemDataRole', Qt).UserRole
_CHECKED = getattr(Qt, 'CheckState', Qt).Checked
_UNCHECKED = getattr(Qt, 'CheckState', Qt).Unchecked
_ACCEPTED = getattr(QDialog, 'DialogCode', QDialog).Accepted


def button(text, callback, layout):
    widget = QPushButton(text)
    widget.clicked.connect(callback)
    layout.addWidget(widget)
    return widget


def guarded(parent, action):
    try:
        return action()
    except Exception as exc:
        QMessageBox.warning(parent, 'Burial operations', str(exc))
        return None


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(getattr(QAbstractItemView, 'SelectionBehavior', QAbstractItemView).SelectRows)
    widget.setEditTriggers(getattr(QAbstractItemView, 'EditTrigger', QAbstractItemView).NoEditTriggers)
    widget.setAlternatingRowColors(True)
    return widget


def fill(widget, rows):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = QTableWidgetItem('' if value is None else str(value))
            widget.setItem(i, j, item)
    widget.resizeColumnsToContents()


class ReportingAxis(pg.AxisItem):
    def __init__(self, template):
        super().__init__(orientation='bottom')
        self.template = template

    def tickStrings(self, values, scale, spacing):
        return [report.tick(v, self.template) for v in values]


class AcquiredDataTab(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        layout = QVBoxLayout(self)
        self.path = QLabel('')
        self.path.setTextInteractionFlags(getattr(Qt, 'TextInteractionFlag', Qt).TextSelectableByMouse)
        layout.addWidget(self.path)
        row = QHBoxLayout()
        button('Open shared GeoPackage…', lambda: guarded(self, self.open_store), row)
        button('New data file…', lambda: guarded(self, self.new_store), row)
        button('Refresh', self.c.refresh, row)
        button('Preview navigation', lambda: guarded(self, lambda: self.c.navigation_layers(self.selected_revision())), row)
        button('Add layers for Explorer', lambda: guarded(self, self.c.add_layers), row)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        button('Import CSV…', lambda: guarded(self, self.import_csv), row)
        button('Import project layer…', lambda: guarded(self, self.import_layer), row)
        self.preset = QComboBox()
        self.preset.setToolTip('Saved field mappings and source conventions')
        row.addWidget(self.preset, 1)
        button('Process selected source…', lambda: guarded(self, self.process), row)
        layout.addLayout(row)
        self.sources = table(['Source', 'Definition', 'Revision', 'Rows', 'Imported UTC', 'Supplier'])
        self.sources.cellDoubleClicked.connect(lambda *_: guarded(self, self.show_source))
        layout.addWidget(self.sources, 2)
        row = QHBoxLayout()
        button('Inspect source', lambda: guarded(self, self.show_source), row)
        button('Save original…', lambda: guarded(self, self.save_original), row)
        self.older = QCheckBox('Show superseded source and processing revisions')
        self.older.toggled.connect(self.refresh)
        row.addWidget(self.older)
        row.addStretch()
        layout.addLayout(row)
        self.revisions = table(['Source', 'Processed UTC', 'Rows', 'Route', 'Status', 'Reason'])
        layout.addWidget(self.revisions, 2)
        row = QHBoxLayout()
        button('Reprocess / revise settings…', lambda: guarded(self, self.reprocess), row)
        button('Correct / split / join a range…', lambda: guarded(self, self.correct_range), row)
        button('Revision history', lambda: guarded(self, self.history), row)
        row.addStretch()
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.status = QLabel('Import a plough file or snapshot an existing Explorer layer. Originals are retained.')
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        button('Cancel operation', self.c.cancel, row)
        layout.addLayout(row)
        self.c.changed.connect(self.refresh)
        self.c.status.connect(self.status.setText)
        self.c.progress.connect(lambda value: self.progress.setValue(int(value)))
        self.c.busyChanged.connect(lambda busy: self.progress.setVisible(busy))
        self.progress.hide()
        self.refresh()

    def refresh(self, *_args):
        selected_source = self.source_rows[self.sources.currentRow()]['id'] if hasattr(self, 'source_rows') and 0 <= self.sources.currentRow() < len(self.source_rows) else None
        self.path.setText('Shared operational data: ' + self.c.path)
        self.source_rows = [r for r in self.c.imports if r['current'] or self.older.isChecked()]
        fill(self.sources, [[r['name'], r['spec']['definition'], ('Current' if r['current'] else 'Superseded') + ' / ' + r['id'][:8],
                             r['count'], r['created'], r['spec'].get('supplier', '')] for r in self.source_rows])
        for i, r in enumerate(self.source_rows):
            if r['id'] == selected_source:
                self.sources.selectRow(i)
        self.processing_rows = [r for r in self.c.revisions if r['current'] or self.older.isChecked()]
        names = {r['id']: r['name'] for r in self.c.imports}
        fill(self.revisions, [[names.get(r['import_id'], ''), r['created'], r['count'], r['route']['name'],
                              'Current' if r['current'] else 'Superseded', r['recipe'].get('reason', '')] for r in self.processing_rows])
        self.preset.clear()
        self.preset.addItem('Auto mapping / no preset', None)
        if self.c.imports:
            latest = {}
            for r in self.c.store().named('template'):
                if r['payload'].get('kind') == 'import':
                    latest[r['name']] = r
            for r in latest.values():
                self.preset.addItem(r['name'], r['payload']['spec'])

    def open_store(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open operational / cable-lay data', '', 'GeoPackage (*.gpkg)')
        if path:
            self.c.open_store(path)

    def new_store(self):
        path, _ = QFileDialog.getSaveFileName(self, 'New operational data', self.c.path, 'GeoPackage (*.gpkg)')
        if path:
            self.c.open_store(path if path.endswith('.gpkg') else path + '.gpkg')

    def save_preset(self, dialog):
        # Reuse the logical source name for an immediately available import preset.
        self.c.store(True).save_named('template', 'Import: ' + dialog.source.text(), {'kind': 'import', 'spec': dialog.value})

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Import acquired burial observations', '', 'Delimited data (*.csv *.txt *.log);;All files (*)')
        if not path:
            return
        content = Path(path).read_bytes()
        spec = self.preset.currentData() or {}
        headers, _ = data.read_csv(content, spec)
        dialog = MappingDialog(headers, Path(path).name, self, spec, content)
        if dialog.exec() == _ACCEPTED:
            self.save_preset(dialog)
            self.c.import_csv(path, dialog.source.text().strip(), dialog.value)

    def import_layer(self):
        layers = [r for r in QgsProject.instance().mapLayers().values() if isinstance(r, QgsVectorLayer)]
        if not layers:
            raise ValueError('Add a vector or table layer to the QGIS project first')
        labels = [f'{r.name()} [{r.id()[:8]}]' for r in layers]
        name, ok = QInputDialog.getItem(self, 'Source layer', 'Snapshot records from:', labels, 0, False)
        if not ok:
            return
        layer = layers[labels.index(name)]
        fields = layer.fields().names()
        spec = copy.deepcopy(self.preset.currentData() or {})
        spec['crs'] = layer.crs().authid() or 'EPSG:4326'
        dialog = MappingDialog([*fields, '__x', '__y'], layer.name(), self, spec)
        if dialog.exec() == _ACCEPTED:
            self.save_preset(dialog)
            # Feature source snapshots are independent of the live layer provider.
            self.c.import_layer(QgsVectorLayerFeatureSource(layer), fields, dialog.source.text().strip(), layer.name(), dialog.value)

    def selected_source(self):
        row = self.sources.currentRow()
        if not 0 <= row < len(self.source_rows):
            raise ValueError('Select a source revision')
        return self.source_rows[row]

    def selected_revision(self):
        row = self.revisions.currentRow()
        if not 0 <= row < len(self.processing_rows):
            raise ValueError('Select a processing revision')
        return self.processing_rows[row]

    def show_source(self):
        row = self.selected_source()
        dialog = JsonDialog('Source revision and import conventions', row, self)
        dialog.editor.setReadOnly(True)
        dialog.exec()

    def save_original(self):
        row = self.selected_source()
        name, content = self.c.store().original(row['id'])
        path, _ = QFileDialog.getSaveFileName(self, 'Save retained original', name)
        if path:
            Path(path).write_bytes(content)

    def process(self):
        row = self.selected_source()
        mapping = row['spec']['mapping']
        recipe = {'kp_mode': 'position' if mapping.get('x') and mapping.get('y') else 'supplied'}
        dialog = ProcessingDialog(self, recipe)
        if dialog.exec() == _ACCEPTED:
            self.c.process(row['id'], dialog.value)

    def reprocess(self):
        row = self.selected_revision()
        dialog = ProcessingDialog(self, row['recipe'])
        if dialog.exec() == _ACCEPTED:
            self.c.process(row['import_id'], dialog.value)

    def correct_range(self):
        revision = self.selected_revision()
        dialog = QDialog(self)
        dialog.setWindowTitle('Derived correction / pass edit')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        lo, hi = spin(0, -100000), spin(1, -100000)
        name = QLineEdit()
        name.setPlaceholderText('Optional: use the same name to join ranges')
        pass_choice = QComboBox()
        pass_choice.addItem('All passes', '')
        for pass_id, pass_name in dict((r['pass_id'], r['pass_name']) for r in self.c.store().samples([revision['id']])).items():
            pass_choice.addItem(pass_name + ' / ' + pass_id, pass_id)
        time_start, time_end = QLineEdit(), QLineEdit()
        for box in (time_start, time_end):
            box.setPlaceholderText('Optional ISO time (UTC)')
        channel = combo(['depth', 'pitch', 'roll', 'tension'], 'depth')
        offset = spin(0, -100000)
        exclude = QCheckBox('Exclude this range from assessment')
        reason = QLineEdit()
        for label, widget in [('Start KP (inclusive)', lo), ('End KP (exclusive)', hi), ('Assign pass name', name),
                              ('Limit to pass', pass_choice), ('From time (inclusive)', time_start), ('Until time (exclusive)', time_end),
                              ('Correction channel', channel), ('Add offset', offset), ('', exclude), ('Reason', reason)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons(dialog, layout)
        if dialog.exec() != _ACCEPTED:
            return
        if lo.value() >= hi.value() or not reason.text().strip():
            raise ValueError('Enter a valid KP range and reason')
        recipe = copy.deepcopy(revision['recipe'])
        recipe.setdefault('edits', []).append({'start_kp': lo.value(), 'end_kp': hi.value(), 'pass_name': name.text().strip(),
                                                'channel': channel.currentText(), 'offset': offset.value(), 'exclude': exclude.isChecked(),
                                                'pass_id': pass_choice.currentData(),
                                                'time_start': data.epoch(time_start.text(), {'timezone': 'UTC'}),
                                                'time_end': data.epoch(time_end.text(), {'timezone': 'UTC'})})
        recipe['reason'] = reason.text().strip()
        self.c.process(revision['import_id'], recipe)

    def history(self):
        dialog = JsonDialog('Operational revision history', self.c.store(True).history(), self)
        dialog.editor.setReadOnly(True)
        dialog.exec()


class AssessmentTab(QWidget):
    def __init__(self, controller, dock, parent=None):
        super().__init__(parent)
        self.c, self.dock = controller, dock
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.revisions = QListWidget()
        self.revisions.setMaximumHeight(100)
        self.revisions.setToolTip('Choose compatible processing revisions. Older revisions remain available in Acquired Data.')
        top.addWidget(self.revisions, 2)
        filters = QFormLayout()
        self.time_start, self.time_end = QLineEdit(), QLineEdit()
        for w in (self.time_start, self.time_end):
            w.setPlaceholderText('ISO date/time; blank = unrestricted')
        self.kp_start, self.kp_end = QLineEdit(), QLineEdit()
        self.zone = combo(['UTC', 'Europe/London', 'UTC+01:00'], 'UTC')
        times = QHBoxLayout()
        times.addWidget(self.time_start)
        times.addWidget(self.time_end)
        kps = QHBoxLayout()
        kps.addWidget(self.kp_start)
        kps.addWidget(self.kp_end)
        filters.addRow('Time start / end', times)
        filters.addRow('KP start / end', kps)
        filters.addRow('Reporting timezone', self.zone)
        top.addLayout(filters, 3)
        layout.addLayout(top)
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem('Selected composite', 'composite')
        self.mode.addItem('All selected passes', 'passes')
        self.mode.addItem('Original measurements at processed KP', 'original')
        self.axis = QComboBox()
        self.axis.addItem('Design KP', 'kp')
        self.axis.addItem('Time', 'time')
        self.step = spin(1, .1, 10000)
        self.method = QComboBox()
        for text, key in [('Station interpolation', 'sample'), ('Interval mean', 'mean'), ('Interval minimum', 'minimum'), ('Interval median', 'median')]:
            self.method.addItem(text, key)
        row.addWidget(self.mode)
        row.addWidget(self.axis)
        row.addWidget(QLabel('Listing step (m)'))
        row.addWidget(self.step)
        row.addWidget(self.method)
        button('Build / refresh view', lambda: guarded(self, self.build), row)
        layout.addLayout(row)
        row = QHBoxLayout()
        button('Use 24 hours', lambda: guarded(self, self.daily), row)
        button('Use 20 km', lambda: guarded(self, self.distance_window), row)
        button('Choose source over range…', lambda: guarded(self, self.override), row)
        layout.addLayout(row)
        row = QHBoxLayout()
        button('Manage selections…', lambda: guarded(self, self.manage_selections), row)
        button('Load saved selection…', lambda: guarded(self, self.load_selection), row)
        button('Compare two passes…', lambda: guarded(self, self.compare), row)
        row.addStretch()
        layout.addLayout(row)
        self.stats = QLabel('Select processed sources and build a view. Time and KP filters can be combined.')
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        self.graph = QWidget()
        self.graph_layout = QVBoxLayout(self.graph)
        self.graph_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.graph, 4)
        self.readout = QLabel('Hover for values; click a plot to inspect the nearest observation and show its KP on the map.')
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        self.ranges = table(['Start KP', 'End KP', 'Maximum shortfall (m)', 'Evidence'])
        self.ranges.setMaximumHeight(130)
        layout.addWidget(self.ranges, 1)
        row = QHBoxLayout()
        button('Create remedial plan from selected ranges…', lambda: guarded(self, self.remedial), row)
        layout.addLayout(row)
        row = QHBoxLayout()
        button('Record remedial status…', lambda: guarded(self, self.remedial_status), row)
        button('Inspect records / provenance', lambda: guarded(self, self.inspect), row)
        button('Cancel', self.c.cancel, row)
        layout.addLayout(row)
        self.c.changed.connect(self.refresh)
        self.c.viewChanged.connect(self.render)
        self.c.status.connect(self.stats.setText)
        self._plots = []
        self._hover_proxies = []
        self.refresh()

    def refresh(self):
        checked = {self.revisions.item(i).data(_USER) for i in range(self.revisions.count())
                   if self.revisions.item(i).checkState() == _CHECKED}
        had_items = self.revisions.count() > 0
        self.revisions.clear()
        names = {r['id']: r['name'] for r in self.c.imports}
        current_fp = self.c.model.route_geometry_fingerprint() if self.c.model.route else ''
        for r in self.c.revisions:
            if r['current']:
                item = QListWidgetItem(f"{names.get(r['import_id'], '')} | {r['created']} | {r['route']['name']}")
                item.setData(_USER, r['id'])
                item.setCheckState(_CHECKED if r['id'] in checked or (not had_items and r['route']['fingerprint'] == current_fp) else _UNCHECKED)
                self.revisions.addItem(item)
        if self.c.snapshot and self.c.stale():
            self.stats.setText('Assessment is stale: plan, source or processing changed. Rebuild before issuing.')

    def query(self):
        query = {}
        for key, box in [('time_start', self.time_start), ('time_end', self.time_end)]:
            if box.text().strip():
                query[key] = data.epoch(box.text(), {'timezone': self.zone.currentText()})
        for key, box in [('kp_start', self.kp_start), ('kp_end', self.kp_end)]:
            if box.text().strip():
                value = data.number(box.text())
                if value is None:
                    raise ValueError('Enter numeric KPs')
                query[key] = value
        return query

    def build(self):
        revisions = [self.revisions.item(i).data(_USER) for i in range(self.revisions.count()) if self.revisions.item(i).checkState() == _CHECKED]
        self.c.template['axis'] = self.axis.currentData()
        self.c.template['timezone'] = self.zone.currentText()
        self.c.build_view(revisions, self.query(), self.step.value(), self.method.currentData(), self.mode.currentData())

    def daily(self):
        start = data.epoch(self.time_start.text(), {'timezone': self.zone.currentText()})
        if start is None:
            raise ValueError('Enter the reporting start time first')
        self.time_end.setText(data.iso(start + 86400))

    def distance_window(self):
        start = data.number(self.kp_start.text())
        if start is None:
            start = float((self.c.model.plan or {}).get('scope_start_kp') or 0)
        self.kp_start.setText(f"{start:.3f}")
        self.kp_end.setText(f"{start + 20:.3f}")

    def render(self):
        self._hover_proxies = []
        self._plots = []
        while self.graph_layout.count():
            widget = self.graph_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        snapshot = self.c.snapshot
        if not snapshot:
            return
        template = snapshot['template']
        self._selected_row = None
        key = 'time' if template['axis'] == 'time' else 'kp'
        rows = snapshot['composite'] if snapshot['mode'] == 'composite' else snapshot['rows']
        self._hover_rows = sorted((r for r in rows if r.get(key) is not None), key=lambda r: r[key])
        self._hover_x = [r[key] for r in self._hover_rows]
        first = None
        for panel in template['panels']:
            plot_widget = pg.PlotWidget(axisItems={'bottom': ReportingAxis(template)} if template['axis'] == 'time' else None)
            plot_widget.setBackground('w')
            self.graph_layout.addWidget(plot_widget, max(1, int(panel.get('height', 1) * 10)))
            plot = plot_widget.getPlotItem()
            plot_widget.scene().sigMouseClicked.connect(lambda event, p=plot: self.clicked(event, p))
            self._hover_proxies.append(pg.SignalProxy(plot_widget.scene().sigMouseMoved, rateLimit=20,
                                                       slot=lambda event, p=plot: self.hover(event, p)))
            plot.showGrid(x=True, y=True, alpha=.2)
            plot.setLabel('left', panel.get('label', ', '.join(panel['channels'])).replace('degrees', '°'))
            plot.getAxis('left').setWidth(65)
            plot.setLabel('bottom', 'Design KP (km)' if template['axis'] == 'kp' else 'Time (' + template.get('timezone', 'UTC') + ')')
            plot.getViewBox().invertY(bool(panel.get('invert')))
            if first is not None:
                plot.setXLink(first)
            first = first or plot
            legend = plot.addLegend(offset=(8, 5))
            seen = set()
            for s in snapshot['plots']:
                if s.get('planned') and panel is template['panels'][0]:
                    region = pg.LinearRegionItem(s['x'], movable=False, brush=pg.mkBrush(120, 145, 160, 35))
                    region.setZValue(-10)
                    region.setToolTip(s['label'])
                    plot.addItem(region, ignoreBounds=True)
                if s.get('event'):
                    marker = pg.InfiniteLine(pos=s['x'][0], angle=90, movable=False, pen=pg.mkPen('#999', style=getattr(Qt, 'PenStyle', Qt).DotLine))
                    marker.setToolTip(s['label'])
                    plot.addItem(marker, ignoreBounds=True)
                if s['channel'] not in panel['channels']:
                    continue
                x = np.asarray([v if v is not None else np.nan for v in s['x']], dtype=float)
                y = np.asarray([v if v is not None else np.nan for v in s['y']], dtype=float)
                pen = pg.mkPen(s['color'], width=1.5)
                if s.get('dashed'):
                    pen.setStyle(getattr(Qt, 'PenStyle', Qt).DashLine)
                name = s['label'] + ' / ' + data.CHANNELS.get(s['channel'], s['channel'])
                curve = plot.plot(x, y, pen=pen, connect='finite', symbol='o' if len(x) == 1 else None, symbolSize=4)
                curve.setDownsampling(auto=True, method='peak')
                curve.setClipToView(True)
                if name not in seen:
                    legend.addItem(curve, name)
                    seen.add(name)
            if panel.get('min') is not None and panel.get('max') is not None:
                plot.setYRange(panel['min'], panel['max'], padding=0)
            crosshair = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#666', style=getattr(Qt, 'PenStyle', Qt).DotLine))
            plot.addItem(crosshair, ignoreBounds=True)
            self._plots.append((plot, crosshair))
        for plot, _ in self._plots[:-1]:
            plot.hideAxis('bottom')
        stats = snapshot['statistics']
        self.stats.setText(f"Unique coverage {stats['covered_km']:.3f} km | Target met {stats['met_km']:.3f} km | "
                           f"Shortfall {stats['shortfall_km']:.3f} km | Missing {stats['missing_km']:.3f} km | "
                           f"Unknown target {stats['unknown_target_km']:.3f} km | {', '.join(snapshot['definitions'])}")
        if len(snapshot['definitions']) > 1:
            self.stats.setText('Composite statistics unavailable: different depth definitions. Compare individual passes.')
        fill(self.ranges, [[f"{r['start_kp']:.3f}", f"{r['end_kp']:.3f}",
                            '' if r['max_shortfall_m'] is None else f"{r['max_shortfall_m']:.3f}", r['kind']] for r in stats['ranges']])

    def nearest(self, position, active_plot=None):
        if not self.c.snapshot:
            return None
        for plot, _ in self._plots:
            if (active_plot is None or plot is active_plot) and plot.sceneBoundingRect().contains(position):
                x = plot.vb.mapSceneToView(position).x()
                if self._hover_x:
                    index = bisect.bisect_left(self._hover_x, x)
                    indexes = {max(0, index - 1), min(index, len(self._hover_x) - 1)}
                    return self._hover_rows[min(indexes, key=lambda i: abs(self._hover_x[i] - x))]
        return None

    def hover(self, event, plot=None):
        row = self.nearest(event[0], plot)
        if row:
            key = 'time' if self.c.snapshot['template']['axis'] == 'time' else 'kp'
            for _, crosshair in self._plots:
                crosshair.setPos(row[key])
            values = ', '.join(f'{data.CHANNELS.get(k, k)} {v:.3f}' for k, v in row['channels'].items() if v is not None)
            kp_text = f"{row['kp']:.3f}" if row.get('kp') is not None else 'unknown'
            self.readout.setText(f"KP {kp_text} | {data.iso(row.get('time'))} | {row['pass_name']} | {values} | "
                                 f"{row['source_file']} row {row['source_row']} | {', '.join(row['flags'])}")

    def clicked(self, event, plot=None):
        row = self.nearest(event.scenePos(), plot)
        if row and row.get('kp') is not None:
            self.dock.highlight_kp(row['kp'])
            self._selected_row = row

    def inspect(self):
        row = getattr(self, '_selected_row', None)
        if row is None:
            if not self.c.snapshot:
                raise ValueError('Build a view first')
            value = self.c.snapshot['rows'][:100]
            title = 'First 100 observations and provenance'
        else:
            value, title = row, 'Selected observation and provenance'
        dialog = JsonDialog(title, value, self)
        dialog.editor.setReadOnly(True)
        dialog.exec()

    def override(self):
        if not self.c.snapshot:
            raise ValueError('Build a view first')
        snapshot = self.c.snapshot
        passes = {(r['processing_id'], r['pass_id']): r for r in snapshot['samples']}
        options = list(passes)
        if not options:
            raise ValueError('No supported passes in the current view')
        dialog = QDialog(self)
        dialog.setWindowTitle('Select evidence for a KP range')
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        start = spin(data.number(self.kp_start.text()) or min(r['kp'] for r in snapshot['samples']), -100000)
        end = spin(data.number(self.kp_end.text()) or max(r['kp'] for r in snapshot['samples']), -100000)
        source = QComboBox()
        for key, row in passes.items():
            source.addItem(f"{row['source_file']} / {row['pass_name']} / {key[0][:8]}", key)
        reason = QLineEdit()
        for label, widget in [('Start KP', start), ('End KP (exclusive)', end), ('Use pass', source), ('Reason', reason)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons(dialog, layout)
        if dialog.exec() == _ACCEPTED:
            if start.value() >= end.value() or not reason.text().strip():
                raise ValueError('Enter a valid KP range and reason')
            revision, pass_id = source.currentData()
            self.c.overrides.append({'start_kp': start.value(), 'end_kp': end.value(), 'processing_id': revision,
                                     'pass_id': pass_id, 'reason': reason.text().strip()})
            self.c.store().save_named('selection', self.c.model.plan_id,
                                     {'overrides': self.c.overrides, 'revisions': snapshot['revisions'], 'route': snapshot['route']})
            self.build()

    def manage_selections(self):
        if not self.c.overrides:
            raise ValueError('No manual source selections in use')
        labels = [f"{o['start_kp']:.3f}–{o['end_kp']:.3f}: {o['pass_id']} ({o['reason']})" for o in self.c.overrides]
        label, ok = QInputDialog.getItem(self, 'Manual source selections', 'Remove selection (retained in revision history):', labels, 0, False)
        if ok:
            reason, ok = QInputDialog.getText(self, 'Remove selection', 'Reason:')
            if ok and reason.strip():
                self.c.overrides.pop(labels.index(label))
                self.c.store().save_named('selection', self.c.model.plan_id,
                                         {'overrides': self.c.overrides, 'route': operations.route_identity(self.c.model),
                                          'revisions': self.c.snapshot['revisions'] if self.c.snapshot else [], 'reason': reason.strip()})
                self.build()

    def load_selection(self):
        rows = self.c.store(True).named('selection')
        if not rows:
            raise ValueError('No saved selections')
        labels = [f"{r['created']} / {r['name']}" for r in rows]
        label, ok = QInputDialog.getItem(self, 'Selection revision', 'Load selection:', labels, len(labels) - 1, False)
        if ok:
            record = rows[labels.index(label)]
            if record['payload']['route'] != operations.route_identity(self.c.model):
                raise ValueError('Selection uses a different design RPL')
            self.c.overrides = record['payload']['overrides']
            for i in range(self.revisions.count()):
                item = self.revisions.item(i)
                item.setCheckState(_CHECKED if item.data(_USER) in record['payload'].get('revisions', []) else _UNCHECKED)
            self.stats.setText('Selection loaded. Check chosen processing revisions and rebuild the view.')

    def compare(self):
        if not self.c.snapshot:
            raise ValueError('Build a view first')
        groups = {}
        for row in self.c.snapshot['samples']:
            groups.setdefault((row['processing_id'], row['pass_id']), []).append(row)
        keys = list(groups)
        labels = [f"{groups[k][0]['source_file']} / {groups[k][0]['pass_name']} / {k[0][:8]}" for k in keys]
        if len(keys) < 2:
            raise ValueError('Select at least two passes')
        first, ok = QInputDialog.getItem(self, 'Compare burial improvement', 'Before:', labels, 0, False)
        if not ok:
            return
        second, ok = QInputDialog.getItem(self, 'Compare burial improvement', 'After:', labels, 1, False)
        if not ok:
            return
        result = data.improvement(groups[keys[labels.index(first)]], groups[keys[labels.index(second)]])
        if not result:
            raise ValueError('No matching supported KPs with compatible depth definitions')
        values = [r['improvement_m'] for r in result]
        QMessageBox.information(self, 'Burial improvement', f'{len(result)} matching stations\nMean improvement: {np.mean(values):.3f} m\n'
                                f'Minimum: {min(values):.3f} m\nMaximum: {max(values):.3f} m\nPositive means deeper burial.')

    def remedial(self):
        if not self.c.snapshot or self.c.stale():
            raise ValueError('Build a current assessment first')
        indexes = sorted({i.row() for i in self.ranges.selectedIndexes()})
        if not indexes:
            raise ValueError('Select one or more assessment ranges')
        ranges = [self.c.snapshot['statistics']['ranges'][i] for i in indexes]
        if any(r['kind'] == 'missing evidence' for r in ranges):
            enum = getattr(QMessageBox, 'StandardButton', QMessageBox)
            if QMessageBox.question(self, 'Missing evidence', 'These ranges have no supported burial measurement. Create a burial plan for them? They may instead need a survey.', enum.Yes | enum.No, enum.No) != enum.Yes:
                return
        name, ok = QInputDialog.getText(self, 'Remedial plan', 'Plan name:', text=(self.c.model.plan.get('name') or 'Burial') + ' remedial')
        if not ok or not name.strip():
            return
        evidence_id = self.c.store().save_issue('Remedial assessment', {'purpose': 'remedial', 'revisions': self.c.snapshot['revisions']}, self.c.snapshot)
        identity = operations.create_remedial_plan(self.c.model, [(r['start_kp'], r['end_kp']) for r in ranges],
                                                   {'operations_gpkg': self.c.path, 'assessment_id': evidence_id}, name.strip())
        self.dock.refresh_plans(identity)
        self.dock.tabs.setCurrentWidget(self.dock.builder_tab)
        self.dock.workflow_tabs.setCurrentIndex(0)

    def remedial_status(self):
        params = json.loads((self.c.model.plan or {}).get('params_json') or '{}')
        remedial = params.get('remedial')
        if not remedial:
            raise ValueError('Open a remedial plan first')
        states = ['planned', 'executed — awaiting verification', 'verification recorded']
        state, ok = QInputDialog.getItem(self, 'Remedial status', 'Status:', states, 0, False)
        if not ok:
            return
        reason, ok = QInputDialog.getText(self, 'Remedial evidence', 'Execution / verification reference and notes:')
        if not ok or not reason.strip():
            return
        evidence = None
        if state == states[2]:
            if not self.c.snapshot or self.c.stale():
                raise ValueError('Build a current assessment of the verification evidence first')
            evidence = self.c.store().save_issue('Remedial verification', {'purpose': 'remedial'}, self.c.snapshot)
        remedial.setdefault('history', []).append({'status': state, 'notes': reason.strip(), 'assessment_id': evidence,
                                                 'processing_revisions': self.c.snapshot['revisions'] if self.c.snapshot else []})
        remedial['status'] = state
        if not self.c.model.update_plan({'params_json': data.serialise(params)}, reason='Remedial status: ' + reason.strip()):
            raise ValueError('Could not save remedial status')
        self.stats.setText('Remedial status: ' + state + '. Compliance remains determined by the assessment.')


class ReportingTab(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.c = controller
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.templates = QComboBox()
        row.addWidget(self.templates, 1)
        button('Load template', lambda: guarded(self, self.load_template), row)
        button('Save template…', lambda: guarded(self, self.save_template), row)
        button('Advanced settings…', lambda: guarded(self, self.advanced), row)
        layout.addLayout(row)
        form = QFormLayout()
        self.title = QLineEdit()
        self.width, self.height, self.margin = spin(297, 100, 1000), spin(210, 100, 1000), spin(8, 0, 100)
        page = QHBoxLayout()
        for label, widget in [('Width', self.width), ('Height', self.height), ('Margin', self.margin)]:
            page.addWidget(QLabel(label + ' (mm)'))
            page.addWidget(widget)
        self.span = spin(20, 0, 100000)
        self.gap_m, self.gap_s = spin(25, .001), spin(300, .001)
        gaps = QHBoxLayout()
        gaps.addWidget(QLabel('Distance (m)'))
        gaps.addWidget(self.gap_m)
        gaps.addWidget(QLabel('Time (s)'))
        gaps.addWidget(self.gap_s)
        form.addRow('Title', self.title)
        form.addRow('Page', page)
        form.addRow('Coverage per page (km / hours; 0 = fit)', self.span)
        form.addRow('Maximum supported gaps', gaps)
        layout.addLayout(form)
        self.panels = QTableWidget(0, 6)
        self.panels.setHorizontalHeaderLabels(['Channels (comma separated)', 'Label / units', 'Height', 'Min (auto blank)', 'Max (auto blank)', 'Depth down'])
        self.panels.setMaximumHeight(165)
        layout.addWidget(self.panels)
        row = QHBoxLayout()
        button('Add panel', self.add_panel, row)
        button('Remove panel', self.remove_panel, row)
        button('Apply layout / preview', lambda: guarded(self, self.apply), row)
        self.page_number = spin(1, 1, 1000, 0)
        self.page_number.valueChanged.connect(self.preview)
        row.addWidget(QLabel('Page'))
        row.addWidget(self.page_number)
        row.addStretch()
        layout.addLayout(row)
        self.summary = QLabel('Build an interactive assessment to preview and export it here.')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.svg = QSvgWidget()
        self.svg.setMinimumHeight(220)
        layout.addWidget(self.svg, 3)
        row = QHBoxLayout()
        button('Export PDF + listing…', lambda: guarded(self, self.export), row)
        button('Re-export saved issue…', lambda: guarded(self, self.reexport), row)
        row.addStretch()
        layout.addLayout(row)
        self.load_controls(self.c.template)
        self.c.viewChanged.connect(self.preview)
        self.c.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.templates.clear()
        self.templates.addItem('Default burial graph', report.DEFAULT_TEMPLATE)
        if self.c.imports:
            latest = {}
            for row in self.c.store().named('template'):
                if row['payload'].get('kind') == 'report':
                    latest[row['name']] = row
            for row in latest.values():
                self.templates.addItem(row['name'], row['payload']['template'])

    def load_controls(self, template):
        self.title.setText(template.get('title', 'Burial graph'))
        for name, widget in [('width_mm', self.width), ('height_mm', self.height), ('margin_mm', self.margin),
                             ('page_span', self.span), ('max_gap_m', self.gap_m), ('max_gap_s', self.gap_s)]:
            widget.setValue(template[name])
        self.panels.setRowCount(0)
        for panel in template['panels']:
            self.add_panel(panel=panel)
        self.panels.resizeColumnsToContents()

    def add_panel(self, _checked=False, panel=None):
        panel = panel or {'channels': ['depth'], 'label': 'Burial Depth (m)', 'height': 1}
        i = self.panels.rowCount()
        self.panels.insertRow(i)
        for j, value in enumerate([', '.join(panel['channels']), panel.get('label', ''), panel.get('height', 1),
                                    panel.get('min', ''), panel.get('max', '')]):
            self.panels.setItem(i, j, QTableWidgetItem('' if value is None else str(value)))
        check = QCheckBox()
        check.setChecked(bool(panel.get('invert')))
        self.panels.setCellWidget(i, 5, check)

    def remove_panel(self):
        if self.panels.currentRow() >= 0:
            self.panels.removeRow(self.panels.currentRow())

    def settings(self):
        template = copy.deepcopy(self.c.template)
        template.update(title=self.title.text(), width_mm=self.width.value(), height_mm=self.height.value(),
                        margin_mm=self.margin.value(), page_span=self.span.value(), max_gap_m=self.gap_m.value(),
                        max_gap_s=self.gap_s.value(), panels=[])
        for i in range(self.panels.rowCount()):
            values = [self.panels.item(i, j).text().strip() for j in range(5)]
            panel = {'channels': [s.strip() for s in values[0].split(',') if s.strip()], 'label': values[1],
                     'height': float(values[2]), 'invert': self.panels.cellWidget(i, 5).isChecked()}
            for key, value in [('min', values[3]), ('max', values[4])]:
                if value:
                    panel[key] = float(value)
            template['panels'].append(panel)
        report.validate_template(template)
        return template

    def apply(self):
        template = self.settings()
        snapshot = self.c.snapshot
        gaps_changed = snapshot and any(template[k] != snapshot['template'][k] for k in ('max_gap_m', 'max_gap_s'))
        self.c.template = template
        if snapshot:
            if gaps_changed:
                self.c.build_view(snapshot['revisions'], snapshot['query'], snapshot['interval_m'], snapshot['method'], snapshot['mode'])
                return
            snapshot['template'] = copy.deepcopy(template)
            snapshot['plots'] = (report.series(snapshot['plotted'], template) + report.target_series(snapshot['composite'], template)
                                 + report.plan_series(snapshot['plan'], template))
            self.c.viewChanged.emit()
        else:
            self.summary.setText('Layout applied. Build a view in Assessment to preview it.')

    def load_template(self):
        template = copy.deepcopy(self.templates.currentData())
        # Data axis/timezone belongs to the current interactive view.
        template['axis'] = self.c.template['axis']
        template['timezone'] = self.c.template['timezone']
        self.c.template = template
        self.load_controls(template)
        self.apply()

    def save_template(self):
        template = self.settings()
        name, ok = QInputDialog.getText(self, 'Save report template', 'Template name:', text=template['title'])
        if ok and name.strip():
            self.c.store(True).save_named('template', name.strip(), {'kind': 'report', 'template': template})
            self.refresh()

    def advanced(self):
        dialog = JsonDialog('Advanced report template', self.settings(), self, report.validate_template)
        if dialog.exec() == _ACCEPTED:
            self.c.template = dialog.value
            self.load_controls(dialog.value)
            self.apply()

    def preview(self, *_args):
        snapshot = self.c.snapshot
        if not snapshot:
            return
        try:
            windows = report.pages(snapshot['plots'], snapshot['template'], report.view_extent(snapshot))
            self.page_number.blockSignals(True)
            self.page_number.setMaximum(len(windows))
            index = min(int(self.page_number.value()) - 1, len(windows) - 1)
            self.page_number.blockSignals(False)
            svg = report.svg_page(snapshot['plots'], snapshot['template'], windows[index],
                                   self.c.subtitle(snapshot), 'Preview — not issued', index + 1, len(windows))
            self.svg.load(svg.encode('utf-8'))
            self.svg.renderer().setAspectRatioMode(getattr(Qt, 'AspectRatioMode', Qt).KeepAspectRatio)
            self.summary.setText(f"Built view: {snapshot['mode']}, {snapshot['interval_m']:g} m {snapshot['method']} listing | "
                                 + self.c.subtitle(snapshot) + (' | STALE: rebuild before export' if self.c.stale() else ''))
        except Exception as exc:
            self.summary.setText(str(exc))

    def export(self):
        if not self.c.snapshot:
            raise ValueError('Build an assessment first')
        self.apply()
        if self.c.task:
            raise ValueError('Wait for the assessment to rebuild with the new settings')
        path, _ = QFileDialog.getSaveFileName(self, 'Export burial graph and listing', 'Burial graph.pdf', 'PDF (*.pdf)')
        if path:
            path = path if path.lower().endswith('.pdf') else path + '.pdf'
            identity = self.c.export(path)
            self.summary.setText(f'Issued {identity[:8]}: PDF, CSV listing and provenance JSON saved together.')

    def reexport(self):
        issues = [r for r in self.c.store(True).issues() if json.loads(r['manifest']).get('purpose') != 'remedial']
        if not issues:
            raise ValueError('No report issues have been saved')
        labels = [f"{r['name']} / {r['created']} / {r['id'][:8]}" for r in issues]
        label, ok = QInputDialog.getItem(self, 'Frozen report issue', 'Re-export:', labels, 0, False)
        if not ok:
            return
        issue = self.c.store().issue(issues[labels.index(label)]['id'])
        path, _ = QFileDialog.getSaveFileName(self, 'Re-export frozen issue', issue['name'] + '.pdf', 'PDF (*.pdf)')
        if path:
            destination = Path(path if path.lower().endswith('.pdf') else path + '.pdf')
            if any(p.exists() for p in (destination, destination.with_suffix('.csv'), destination.with_suffix('.json'))):
                raise ValueError('Choose a new output name')
            snapshot = issue['snapshot']
            operations.export_pdf(destination, snapshot['plots'], snapshot['template'], self.c.subtitle(snapshot), 'Issue ' + issue['id'], report.view_extent(snapshot))
            rows = snapshot['composite'] if snapshot['mode'] == 'composite' else snapshot['samples']
            destination.with_suffix('.csv').write_bytes(report.listing_csv(rows).encode('utf-8'))
            manifest = dict(issue['manifest'], reexport_of=issue['id'], original_pdf_sha256=issue['manifest'].get('pdf_sha256'))
            manifest['pdf_sha256'] = hashlib.sha256(destination.read_bytes()).hexdigest()
            manifest['listing_sha256'] = hashlib.sha256(destination.with_suffix('.csv').read_bytes()).hexdigest()
            destination.with_suffix('.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            self.summary.setText('Re-exported the frozen data and template from issue ' + issue['id'][:8])
