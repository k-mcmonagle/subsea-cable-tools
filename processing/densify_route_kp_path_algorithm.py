# -*- coding: utf-8 -*-
"""Densify Route Along KP Path (docs/KP_METHOD.md, sections 2-3).

A map draws a line straight in its CRS between vertices, while KP is measured
along the route's KP path: the geodesic between vertices (or the straight
line in the grid for grid KP). On long legs stored in latitude/longitude the
two are far apart, so KP markers sit off the drawn line. This tool writes a
display copy of the route with points added along its KP path, so it is drawn
where its KPs lie in any map CRS. The input is not changed.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Sequence, Set, Tuple

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsWkbTypes,
)

from .algorithm_base import SubseaCableAlgorithm
from ..kp_geo_utils import PATH_DRAWN, RouteFrame, get_features_skip_invalid, iter_line_parts, ordered_route_features
from ..kp_range_utils import add_distance_mode_parameter, make_distance_area, read_distance_mode
from ..qgis_compat import FIELD_TYPE_DOUBLE
from .kp_common import START_KP_HELP, add_start_kp_parameter, build_route, read_start_kp, report_kp_basis
from .rpl_comparison_utils import feature_frames, kp_path_geometry

_Key = Tuple[float, float]


def unique_field_name(fields: QgsFields, base: str) -> str:
    """``base``, or ``base_2``, ``base_3``... when ``fields`` already has that
    name (compared without case: most formats ignore it)."""
    taken = {name.lower() for name in fields.names()}
    if base.lower() not in taken:
        return base
    index = 2
    while f"{base}_{index}".lower() in taken:
        index += 1
    return f"{base}_{index}"


def _key(point) -> _Key:
    return (float(point.x()), float(point.y()))


def _stored_parts(geom: QgsGeometry) -> List[List[QgsPointXY]]:
    """The feature's line parts as stored, 2D, parts of at least two vertices."""
    return [[QgsPointXY(p) for p in part] for part in iter_line_parts(geom) if len(part) >= 2]


def _required_vertices(parts: Sequence[Sequence[QgsPointXY]]) -> Set[_Key]:
    """Vertices the KP path must keep: those of parts with any length (a part
    whose vertices all coincide has no KP path and is not drawn)."""
    keys: Set[_Key] = set()
    for part in parts:
        part_keys = [_key(p) for p in part]
        if len(set(part_keys)) >= 2:
            keys.update(part_keys)
    return keys


def _path_parts(geom: QgsGeometry) -> List[List[QgsPointXY]]:
    if geom is None or geom.isEmpty():
        return []
    if geom.isMultipart():
        return [list(part) for part in geom.asMultiPolyline() if len(part) >= 2]
    part = geom.asPolyline()
    return [list(part)] if len(part) >= 2 else []


def _added_runs(parts: Sequence[Sequence[QgsPointXY]], stored: Set[_Key]) -> List[List[QgsPointXY]]:
    """Runs of added points: the points between two stored vertices."""
    runs: List[List[QgsPointXY]] = []
    for part in parts:
        run: List[QgsPointXY] = []
        for point in part:
            if _key(point) in stored:
                if run:
                    runs.append(run)
                run = []
            else:
                run.append(point)
        if run:
            runs.append(run)
    return runs


def _distance_to(drawn: RouteFrame) -> Callable[[QgsPointXY], float]:
    """Distance (m, as KP measures it) from a point to the line as stored
    (``drawn``: a RouteFrame that keeps the stored segments)."""
    def offset_at(point: QgsPointXY) -> float:
        return drawn.kp_at_point(QgsPointXY(point)).dcc_m
    return offset_at


def _largest_offset_m(run: Sequence[QgsPointXY], offset_at: Callable[[QgsPointXY], float]) -> float:
    """The largest of ``offset_at`` over one run of added points.

    Between two vertices the KP path departs from the stored segment and comes
    back once, so a coarse-to-fine search (nine samples, then around the
    largest) finds the largest offset without measuring every point."""
    cache: Dict[int, float] = {}

    def value(index: int) -> float:
        if index not in cache:
            try:
                offset = float(offset_at(run[index]))
            except Exception:  # noqa: BLE001 - a report, never a failure
                offset = 0.0
            cache[index] = offset if math.isfinite(offset) else 0.0
        return cache[index]

    lo, hi = 0, len(run) - 1
    while True:
        step = max(1, (hi - lo) // 8)
        best = max(sorted(set(range(lo, hi + 1, step)) | {hi}), key=value)
        if step == 1:
            return value(best)
        lo, hi = max(lo, best - step), min(hi, best + step)


class DensifyRouteKpPathAlgorithm(SubseaCableAlgorithm):
    """A display copy of a route with points along its KP path."""

    INPUT = 'INPUT'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Reference Line Layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )
        add_distance_mode_parameter(self)
        add_start_kp_parameter(self)
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Route along KP path'),
                QgsProcessing.TypeVectorLine,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        source_crs = source.sourceCrs()

        fields = QgsFields(source.fields())
        kp_from_name = unique_field_name(fields, 'kp_from')
        fields.append(QgsField(kp_from_name, FIELD_TYPE_DOUBLE, '', 20, 6))
        kp_to_name = unique_field_name(fields, 'kp_to')
        fields.append(QgsField(kp_to_name, FIELD_TYPE_DOUBLE, '', 20, 6))
        for base, name in (('kp_from', kp_from_name), ('kp_to', kp_to_name)):
            if name != base:
                feedback.pushInfo(f"The input already has a field called {base}: the KP is written to {name}.")

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.MultiLineString, source_crs)
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        all_features = list(get_features_skip_invalid(source))
        features = ordered_route_features(all_features)
        if len(all_features) > len(features):
            feedback.pushInfo(f"{len(all_features) - len(features)} feature(s) without a line geometry "
                              "were left out.")
        if not features:
            feedback.pushWarning("The input has no line features: the output is empty.")
            return {self.OUTPUT: dest_id}

        try:
            distance = make_distance_area(
                source_crs, context.transformContext(),
                mode=read_distance_mode(self, parameters, context), project=context.project())
        except ValueError as exc:
            raise QgsProcessingException(str(exc)) from exc

        # One route over the features in route order (SeqNo when every feature
        # has one, else layer order); each feature's KPs are its KPs on it.
        start_kp = read_start_kp(self, parameters, context)
        route = build_route([QgsGeometry(f.geometry()) for f in features], distance, start_kp)
        report_kp_basis(feedback, route, start_kp)

        added_total = 0
        densified = 0
        largest_m = 0.0
        unmeasured: List[int] = []
        total = len(features)
        for index, (feat, frame) in enumerate(zip(features, feature_frames(route, distance))):
            if feedback.isCanceled():
                break
            feedback.setProgress(int(100 * index / total))

            stored = _stored_parts(feat.geometry())
            required = _required_vertices(stored)
            parts = _path_parts(kp_path_geometry(frame))
            path_keys = {_key(p) for part in parts for p in part}
            if not parts or not required <= path_keys:
                # No KP path (a line of zero length), or one that stops short
                # of a vertex (a leg that cannot be measured): keep the line
                # as stored rather than drop any of it.
                if required:
                    unmeasured.append(index + 1)
                parts = stored
            else:
                runs = _added_runs(parts, required)
                added = sum(len(run) for run in runs)
                if added:
                    added_total += added
                    densified += 1
                    # How far the KP path is from the line as stored: the
                    # distance (as KP measures it) of the added points to it.
                    offset_at = _distance_to(
                        RouteFrame.from_source([QgsGeometry(feat.geometry())], distance, path=PATH_DRAWN))
                    for run in runs:
                        largest_m = max(largest_m, _largest_offset_m(run, offset_at))

            out = QgsFeature(fields)
            out.setGeometry(QgsGeometry.fromMultiPolylineXY(parts) if parts else QgsGeometry())
            out.setAttributes(list(feat.attributes()) + [round(frame.start_kp_km, 6), round(frame.end_kp_km, 6)])
            sink.addFeature(out, QgsFeatureSink.FastInsert)

        if unmeasured:
            feedback.pushWarning(
                f"Feature(s) {', '.join(str(n) for n in unmeasured)} in route order: the KP path could not be "
                "computed to every vertex (a leg that cannot be measured); written as stored.")
        if added_total:
            feedback.pushInfo(
                f"Added {added_total} point(s) along the KP path to {densified} of {total} feature(s); "
                "every input vertex is kept.")
            feedback.pushInfo(
                f"The KP path is up to {largest_m:.1f} m from the input as stored (straight between vertices "
                f"in {source_crs.authid() or 'its CRS'}).")
        else:
            feedback.pushInfo(
                "No points were added: between every pair of vertices the KP path is within 1 mm of the input "
                "as stored, or the leg is too short for a point every 250 m.")

        feedback.setProgress(100)
        return {self.OUTPUT: dest_id}

    def shortHelpString(self):
        return self.tr("""
Writes a copy of a route line layer with points added along its KP path, so the route is drawn where its KPs lie in any map CRS.

**Why:** a map draws a line straight in its CRS between vertices, but KP is measured along the KP path: between vertices along the geodesic (or the straight line in the grid for grid KP). On long legs the two differ. Mid-way along a 100 km leg at 57.5°N, a line drawn straight in latitude/longitude is about 350 m from the geodesic, so KP points, KP ranges and crossings sit off the drawn line.

**Output:**
- Points about every 250 m, added only where the KP path departs from the drawn segment by more than 1 mm.
- The original vertices, and their KPs, are unchanged. KP measured along the output equals KP along the input to within millimetres.
- One feature per input feature, in route order (SeqNo when every feature has one, else layer order), with all input attributes plus kp_from and kp_to: the KP (km) where the feature starts and ends on the whole route (start KP + chainage). If the input already has a field of that name, the next free name is used (for example kp_from_2).
- Heights (Z) and measures (M) are not kept.
- The log states how many points were added and how far the KP path is from the input as drawn.

Use the output for display and overlays. The input RPL is not changed.
""") + "\n\n" + self.tr(START_KP_HELP)

    def name(self):
        return 'densifyroutekppath'

    def displayName(self):
        return self.tr('Densify Route Along KP Path')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return DensifyRouteKpPathAlgorithm()
