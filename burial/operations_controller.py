# -*- coding: utf-8 -*-
"""Shared lifecycle state for acquired data, assessment and reporting tabs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from qgis.core import QgsApplication, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, pyqtSignal

from ..qgis_compat import FIELD_TYPE_STRING, FIELD_TYPE_DOUBLE
from ..laydata import burial_data as data, burial_report as report
from ..laydata.burial_store import BurialDataStore, new_id
from . import operations, target_depth


class OperationsController(QObject):
    changed = pyqtSignal()
    viewChanged = pyqtSignal()
    status = pyqtSignal(str)
    busyChanged = pyqtSignal(bool)
    progress = pyqtSignal(float)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.task = None
        self.generation = 0
        self.snapshot = None
        self.imports, self.revisions = [], []
        self.template = copy.deepcopy(report.DEFAULT_TEMPLATE)
        self.overrides = []
        self._last_path = ''
        self._context = ''
        self.model.planChanged.connect(self.refresh)

    @property
    def path(self):
        project = QgsProject.instance()
        value, _ = project.readEntry('SubseaCableTools', 'burial_operations_gpkg', '')
        if value:
            return project.pathResolver().readPath(value)
        shared, _ = project.readEntry('SubseaCableTools', 'cable_lay_gpkg', '')
        if shared:
            return project.pathResolver().readPath(shared)
        return os.path.splitext(self.model.store.gpkg_path)[0] + '_operations.gpkg'

    def store(self, ensure=False):
        store = BurialDataStore(self.path)
        if ensure:
            store.ensure()
            project = QgsProject.instance()
            project.writeEntry('SubseaCableTools', 'burial_operations_gpkg', project.pathResolver().writePath(self.path))
        return store

    def open_store(self, path):
        if self.task:
            raise ValueError('Finish or cancel the current operation first')
        BurialDataStore(path).ensure()
        project = QgsProject.instance()
        project.writeEntry('SubseaCableTools', 'burial_operations_gpkg', project.pathResolver().writePath(path))
        self.snapshot = None
        self.overrides = []
        self.refresh()
        self.viewChanged.emit()

    def context(self):
        return data.fingerprint({'plan': self.model.plan, 'route': self.model.route_geometry_fingerprint()
                                 if self.model.route else ''})

    def refresh(self, *_args):
        path = self.path
        context = self.context()
        changed_context = context != self._context or path != self._last_path
        if path != self._last_path:
            self.cancel()
            self.snapshot = None
            self.overrides = []
        self._last_path, self._context = path, context
        self.imports, self.revisions = [], []
        if os.path.exists(path):
            try:
                self.imports = self.store().imports()
                self.revisions = self.store().revisions()
            except Exception as exc:
                # An existing Explorer GeoPackage may not yet contain bd_* tables.
                if 'no such table: bd_' not in str(exc):
                    self.status.emit(str(exc))
        if changed_context and self.snapshot:
            self.status.emit('Plan or route changed. Rebuild the assessment before exporting.')
        self.changed.emit()

    def start(self, title, work, callback=None):
        if self.task:
            raise ValueError('An operation is already running')
        self.generation += 1
        generation = self.generation
        self.task = operations.OperationTask(title, work)
        task = self.task
        self.busyChanged.emit(True)
        self.status.emit(title)
        task.progressChanged.connect(self.progress.emit)
        def done(result):
            if generation != self.generation:
                return
            self.task = None
            self.busyChanged.emit(False)
            self.refresh()
            if callback:
                callback(result)
            if not callback:
                self.status.emit('Completed; rebuild the assessment to include changes.')
        def failed(message):
            if generation != self.generation:
                return
            self.task = None
            self.busyChanged.emit(False)
            self.status.emit(message)
        task.completed.connect(done)
        task.failed.connect(failed)
        QgsApplication.taskManager().addTask(task)

    def cancel(self):
        self.generation += 1
        if self.task:
            self.task.cancel()
            self.task = None
        self.busyChanged.emit(False)
        self.status.emit('Operation cancelled. Committed revisions are retained.')

    def import_csv(self, path, source_key, spec):
        store = self.store(True)
        spec = copy.deepcopy(spec)
        def work(task):
            content = Path(path).read_bytes()
            return store.ingest(content, source_key, os.path.basename(path), spec, cancelled=task.isCanceled)
        self.start('Import burial source', work)

    def import_layer(self, source, fields, source_key, name, spec):
        store = self.store(True)
        spec = copy.deepcopy(spec)
        def work(task):
            records = []
            for f in source.getFeatures():
                if task.isCanceled():
                    raise InterruptedError('Import cancelled')
                row = {}
                for field in fields:
                    value = f[field]
                    if hasattr(value, 'toString'):
                        value = value.toString('yyyy-MM-ddTHH:mm:ss.zzz')
                    if not isinstance(value, (str, float, int, bool)):
                        value = None
                    if isinstance(value, float) and data.number(value) is None:
                        value = None
                    row[field] = value
                if f.hasGeometry() and not f.geometry().isEmpty():
                    point = f.geometry().centroid().asPoint()
                    row['__x'], row['__y'] = point.x(), point.y()
                row['_source_row'] = f.id()
                records.append(row)
            content = data.serialise(records).encode('utf-8')
            return store.ingest(content, source_key, name + '.json', spec, records, task.isCanceled)
        self.start('Snapshot project layer', work)

    def process(self, import_id, recipe):
        record = next(r for r in self.imports if r['id'] == import_id)
        recipe = copy.deepcopy(recipe)
        recipe['route'] = operations.route_identity(self.model)
        recipe['version'] = data.VERSION
        projector = operations.RouteProjector(self.model, record['spec'].get('crs', 'EPSG:4326'),
                                              recipe.get('ambiguity_m', 0)) if recipe.get('kp_mode') == 'position' else None
        store = self.store(True)
        def work(task):
            rows = data.process(store.observations(import_id), recipe, projector, task.isCanceled, task.setProgress)
            return store.save_processing(import_id, recipe, rows, task.isCanceled)
        self.start('Process burial pass revision', work)

    def build_view(self, revisions, query, interval_m, method, mode='composite'):
        if not revisions:
            raise ValueError('Select at least one processing revision')
        current_route = operations.route_identity(self.model)
        selected = [r for r in self.revisions if r['id'] in revisions]
        if len(selected) != len(set(revisions)) or any(r['route'] != current_route for r in selected):
            raise ValueError('Selected processing uses another RPL revision. Reprocess it against this plan first.')
        query, template, overrides = copy.deepcopy(query), copy.deepcopy(self.template), copy.deepcopy(self.overrides)
        report.validate_template(template)
        plan = copy.deepcopy(self.model.plan)
        context = self.context()
        store = self.store(True)
        def work(task):
            rows = store.samples(revisions)
            if mode == 'original':
                rows = [dict(r, channels=dict(r.get('original_channels', r['channels'])),
                             valid=r.get('kp') is not None and r.get('original_channels', r['channels']).get('depth') is not None)
                        for r in rows]
            selected_rows = data.select_rows(rows, query)
            if not selected_rows:
                raise ValueError('No observations in this selection')
            samples = data.stations(rows, query, interval_m, template['max_gap_m'], template['max_gap_s'], method, task.isCanceled)
            definitions = sorted({r['definition'] for r in samples})
            if len(definitions) > 1 and mode == 'composite':
                raise ValueError('Select sources with the same depth definition for a composite; use All passes to compare definitions')
            combined = data.composite(samples, overrides) if len(definitions) <= 1 else []
            referenced = {(r['processing_id'], r['pass_id']) for r in rows}
            unresolved = [o for o in overrides if (o.get('processing_id'), o['pass_id']) not in referenced]
            if unresolved and mode == 'composite':
                raise ValueError('A manual selection references an unavailable pass/revision. Load that revision or revise the selection.')
            ranges = target_depth.plan_ranges(plan)
            default = target_depth.plan_default(plan)
            scope = (query.get('kp_start', plan['scope_start_kp']), query.get('kp_end', plan['scope_end_kp']))
            scope = (plan['scope_start_kp'] if scope[0] is None else scope[0], plan['scope_end_kp'] if scope[1] is None else scope[1])
            statistics = data.assess(combined, lambda kp: target_depth.target_at(kp, default, ranges), scope if len(definitions) <= 1 else None,
                                     [r[k] for r in ranges for k in ('start_kp', 'end_kp')])
            plotted = combined if mode == 'composite' else selected_rows
            plots = report.series(plotted, template) + report.plan_series(plan, template)
            if 'depth' in [c for p in template['panels'] for c in p['channels']]:
                plots += report.target_series(combined, template)
            report.bounds(plots)
            return dict(rows=selected_rows, plotted=plotted, samples=samples, composite=combined, plots=plots, statistics=statistics,
                        query=query, template=template, overrides=overrides, revisions=revisions, plan=plan,
                        context=context, route=current_route, mode=mode, interval_m=interval_m, method=method,
                        definitions=definitions, source_revisions=[r['import_id'] for r in selected])
        def complete(result):
            self.snapshot = result
            self.viewChanged.emit()
        self.start('Build burial assessment', work, complete)

    def stale(self):
        if not self.snapshot:
            return True
        if self.snapshot['context'] != self.context():
            return True
        current = {r['id'] for r in self.revisions if r['current']}
        return any(r not in current for r in self.snapshot['revisions'])

    def add_layers(self):
        store = self.store(True)
        project = QgsProject.instance()
        for table, name in [('bd_current_observations', 'Burial original observations'),
                            ('bd_current_samples', 'Burial processed observations')]:
            uri = store.path.replace('\\', '/') + f'|layername={table}'
            layer = next((v for v in project.mapLayers().values() if v.source().replace('\\', '/') == uri), None)
            if layer is None:
                layer = QgsVectorLayer(uri, name, 'ogr')
                if not layer.isValid():
                    raise ValueError(f'Could not open {table}')
                layer.setReadOnly(True)
                project.addMapLayer(layer)
            else:
                layer.reload()
            layer.triggerRepaint()

    def navigation_layers(self, revision):
        """Review original/filtered positions without snapping away cross-track errors."""
        source = next(r for r in self.imports if r['id'] == revision['import_id'])
        rows = self.store().samples([revision['id']])
        if not any(r.get('original_x') is not None and r.get('original_y') is not None for r in rows):
            raise ValueError('This source contains no positions')
        project = QgsProject.instance()
        for label, xkey, ykey, color in [('Original', 'original_x', 'original_y', '#999999'),
                                          ('Processed', 'x', 'y', '#1565c0')]:
            layer = QgsVectorLayer('Point?crs=' + source['spec'].get('crs', 'EPSG:4326'),
                                   f"Burial {label} navigation / {source['name']} / {revision['id'][:8]}", 'memory')
            provider = layer.dataProvider()
            provider.addAttributes([QgsField('observation_id', FIELD_TYPE_STRING), QgsField('design_kp', FIELD_TYPE_DOUBLE),
                                     QgsField('cross_track_m', FIELD_TYPE_DOUBLE), QgsField('flags', FIELD_TYPE_STRING)])
            layer.updateFields()
            features = []
            for row in rows:
                if row.get(xkey) is None or row.get(ykey) is None:
                    continue
                feature = QgsFeature(layer.fields())
                feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(row[xkey], row[ykey])))
                feature.setAttributes([row['observation_id'], row.get('kp'), row.get('cross_track_m'), ', '.join(row['flags'])])
                features.append(feature)
            provider.addFeatures(features)
            layer.updateExtents()
            from qgis.PyQt.QtGui import QColor
            layer.renderer().symbol().setColor(QColor(color))
            layer.renderer().symbol().setSize(1.5)
            layer.setCustomProperty('subsea/burial_processing_id', revision['id'])
            layer.setReadOnly(True)
            project.addMapLayer(layer)

    def export(self, pdf_path, allow_stale=False):
        if not self.snapshot:
            raise ValueError('Build an assessment first')
        if self.stale() and not allow_stale:
            raise ValueError('Sources, plan or route changed. Rebuild before issuing a new report.')
        snapshot = copy.deepcopy(self.snapshot)
        store = self.store(True)
        identity = new_id()
        manifest = {k: snapshot[k] for k in ('revisions', 'source_revisions', 'query', 'route', 'overrides', 'mode', 'interval_m', 'method')}
        manifest['template'] = snapshot['template']
        manifest['plan'] = snapshot['plan']
        manifest['software_version'] = data.VERSION
        destination = Path(pdf_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        listing_path = destination.with_suffix('.csv')
        manifest_path = destination.with_suffix('.json')
        if any(p.exists() for p in (destination, listing_path, manifest_path)):
            raise ValueError('Choose a new output name; issued files are not overwritten')
        with tempfile.TemporaryDirectory(dir=str(destination.parent)) as temp:
            temp_pdf = Path(temp) / 'report.pdf'
            subtitle = self.subtitle(snapshot)
            operations.export_pdf(temp_pdf, snapshot['plots'], snapshot['template'], subtitle, 'Issue ' + identity, report.view_extent(snapshot))
            listing_rows = snapshot['composite'] if snapshot['mode'] == 'composite' else snapshot['samples']
            text = report.listing_csv(listing_rows)
            manifest['issue_id'] = identity
            manifest['pdf_sha256'] = hashlib.sha256(temp_pdf.read_bytes()).hexdigest()
            manifest['listing_sha256'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
            # Freeze the complete plotted data before publishing the output files.
            store.save_issue(destination.stem, manifest, snapshot, identity)
            temp_listing, temp_manifest = Path(temp) / 'listing.csv', Path(temp) / 'manifest.json'
            temp_listing.write_bytes(text.encode('utf-8'))
            temp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            published = []
            try:
                for source, target in ((temp_pdf, destination), (temp_listing, listing_path), (temp_manifest, manifest_path)):
                    os.replace(source, target)
                    published.append(target)
            except OSError:
                for target in published:
                    target.unlink(missing_ok=True)
                raise
        self.changed.emit()
        return identity

    @staticmethod
    def subtitle(snapshot):
        plan = snapshot['plan']
        query = snapshot['query']
        period = ' to '.join(data.iso(query.get(k)) or 'open' for k in ('time_start', 'time_end'))
        definition = ', '.join(snapshot.get('definitions', []))
        return f"{plan.get('name', '')} | {plan.get('rpl_name', '')} {plan.get('rpl_revision', '')} | {snapshot['mode']} | {period} | {definition}"
