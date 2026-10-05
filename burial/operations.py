# -*- coding: utf-8 -*-
"""QGIS adapters for acquired burial processing, vector PDF and remedial plans."""
from __future__ import annotations

import json
import math
from pathlib import Path

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsGeometry, QgsPointXY, QgsProject, QgsTask)
from qgis.PyQt.QtCore import QMarginsF, QRectF, QSizeF, pyqtSignal
from qgis.PyQt.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
from qgis.PyQt.QtSvg import QSvgRenderer

from ..kp_geo_utils import RouteFrame
from ..laydata import burial_data as data
from ..laydata import burial_report as report
from . import io_csv, schema
from .plan_model import PlanModel


class OperationTask(QgsTask):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, title, work):
        super().__init__(title, getattr(QgsTask, 'Flag', QgsTask).CanCancel)
        self.work, self.result, self.error = work, None, ''

    def run(self):
        try:
            self.result = self.work(self)
            return True
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            return False

    def finished(self, success):
        if success:
            self.completed.emit(self.result)
        else:
            self.failed.emit(self.error or 'Cancelled')


def route_identity(model):
    if model.route is None:
        raise ValueError('Set a design RPL on the plan first')
    route = model.route
    mode, grid = model.kp_mode()
    return {'fingerprint': model.route_geometry_fingerprint(), 'rpl_id': model.plan.get('rpl_id', ''),
            'name': model.plan.get('rpl_name', ''), 'revision': model.plan.get('rpl_revision', ''),
            'mode': mode, 'grid_crs': grid, 'start_kp': route.start_kp_km,
            'geometry_wkt': [g.asWkt(16) for g in route._geoms],
            'feature_lengths_m': list(route._feature_lengths_m)}


class RouteProjector:
    """Private immutable route and CRS snapshots, safe to use inside a task."""
    def __init__(self, model, source_crs, ambiguity_m=0):
        source = model.route
        if source is None:
            raise ValueError('The current plan has no design RPL')
        distance = source._distance.clone() if hasattr(source._distance, 'clone') else QgsDistanceArea(source._distance)
        self.route = RouteFrame([QgsGeometry(g) for g in source._geoms], list(source._feature_lengths_m),
                                distance, start_kp_km=source.start_kp_km)
        crs = QgsCoordinateReferenceSystem(source_crs)
        if not crs.isValid():
            raise ValueError('Choose a valid input CRS')
        self.transform = QgsCoordinateTransform(crs, distance.sourceCrs(), QgsProject.instance().transformContext())
        self.ambiguity_m = float(ambiguity_m)

    def __call__(self, x, y):
        point = self.transform.transform(QgsPointXY(x, y))
        hit = self.route.kp_at_point(point)
        if hit.feature_index < 0 or not math.isfinite(hit.kp_km):
            return None, None, ['missing_kp']
        flags = []
        if self.ambiguity_m > 0:
            # Nearby, non-adjacent route segments may represent a crossing or parallel cable.
            ids = self.route._kp_index.nearestNeighbor(point, 12)
            for index in ids:
                a, b, length, start = self.route._segs[index]
                dx, dy = b.x() - a.x(), b.y() - a.y()
                square = dx * dx + dy * dy
                if square <= 0:
                    continue
                fraction = max(0, min(1, ((point.x()-a.x())*dx + (point.y()-a.y())*dy) / square))
                candidate = QgsPointXY(a.x() + fraction * dx, a.y() + fraction * dy)
                offset = self.route._distance.measureLine(point, candidate)
                kp = self.route.start_kp_km + (start + fraction * length) / 1000
                if offset <= abs(hit.dcc_m) + self.ambiguity_m and abs(kp - hit.kp_km) * 1000 > max(10, self.ambiguity_m * 2):
                    flags.append('ambiguous_kp')
                    break
        return float(hit.kp_km), float(hit.dcc_m), flags


def export_pdf(path, plot_series, template, subtitle='', footer='', extent=None):
    """Render the same SVG page model as the report preview into a vector PDF."""
    windows = report.pages(plot_series, template, extent)
    writer = QPdfWriter(str(path))
    unit = getattr(QPageSize, 'Unit', QPageSize).Millimeter
    layout_unit = getattr(QPageLayout, 'Unit', QPageLayout).Millimeter
    writer.setPageSize(QPageSize(QSizeF(template['width_mm'], template['height_mm']), unit))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), layout_unit)
    writer.setResolution(144)
    writer.setTitle(template.get('title', 'Burial graph'))
    writer.setCreator('Subsea Cable Tools')
    painter = QPainter()
    if not painter.begin(writer):
        raise OSError('Could not open PDF output')
    try:
        for index, window in enumerate(windows):
            if index and not writer.newPage():
                raise OSError('Could not create PDF page')
            svg = report.svg_page(plot_series, template, window, subtitle, footer, index + 1, len(windows))
            renderer = QSvgRenderer(svg.encode('utf-8'))
            if not renderer.isValid():
                raise ValueError('Invalid report page')
            renderer.render(painter, QRectF(0, 0, writer.width(), writer.height()))
    finally:
        painter.end()
    if not Path(path).is_file() or Path(path).stat().st_size == 0:
        raise OSError('PDF export produced no output')
    return len(windows)


def create_remedial_plan(model, ranges, evidence, name):
    if not model.plan or not ranges:
        raise ValueError('Choose assessment ranges and a parent plan')
    parent = dict(model.plan)
    lo, hi = sorted((parent['scope_start_kp'], parent['scope_end_kp']))
    for start, end in ranges:
        if start < lo or end > hi or start >= end:
            raise ValueError('Remedial ranges must be within the parent plan scope')
    # Union overlapping/adjacent requests before creating event pairs.
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    child = None
    committed = False
    try:
        with model.store.transaction():
            identity = model.store.duplicate_plan(model.plan_id, name)
            child = PlanModel(model.store, model.workbench_store)
            try:
                if child.load_plan(identity) is False:
                    raise ValueError('Could not open the new remedial plan')
                params = json.loads(child.plan.get('params_json') or '{}')
                params['remedial'] = dict(parent_plan_id=parent['plan_id'], evidence=evidence,
                                          ranges=merged, status='planned')
                if not child.update_plan({'params_json': data.serialise(params), 'status': schema.PLAN_STATUS_DRAFT,
                                          'description': 'Remedial burial based on acquired data assessment'},
                                         reason='Create remedial plan from assessment'):
                    raise ValueError('Could not save remedial provenance')
                events = io_csv.parse_kp_ranges_csv('start_kp,end_kp\n' + '\n'.join(f'{a},{b}' for a, b in merged))
                if child.direction < 0:
                    for event in events:
                        event['event_type'] = (schema.EVENT_BURIAL_END if event['event_type'] == schema.EVENT_BURIAL_START
                                               else schema.EVENT_BURIAL_START)
                if not child.import_plan(events, 'remedial assessment', reason='Selected acquired burial shortfalls'):
                    raise ValueError('Could not create remedial event pairs')
            finally:
                child._layer_timer.stop()
        committed = True
    finally:
        if child is not None:
            if not committed:
                child._pending_layer_parts.clear()
            child.close_plan()
    return identity
