# place_single_kp_point_algorithm.py
# -*- coding: utf-8 -*-
"""
PlaceSingleKpPointAlgorithm
This tool places a single KP point along a route.
"""

import math

from qgis.PyQt.QtCore import QCoreApplication, QSettings
from .algorithm_base import SubseaCableAlgorithm
from ..kp_range_utils import (
    make_distance_area,
    add_distance_mode_parameter,
    read_distance_mode,
)
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterBoolean,
                       QgsProcessingParameterEnum,
                       QgsProcessingParameterNumber,
                       QgsProcessingParameterFeatureSink,
                       QgsFeature,
                       QgsGeometry,
                       QgsPointXY,
                       QgsFields,
                       QgsField,
                       QgsWkbTypes,
                       QgsProcessingException,
                       QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform)
from ..qgis_compat import FIELD_TYPE_DOUBLE, FIELD_TYPE_STRING, PROCESSING_NUMBER_DOUBLE
from ..kp_geo_utils import get_features_skip_invalid, kp_path_kind, ordered_route_geometry
from .kp_common import START_KP_HELP, add_start_kp_parameter, build_route, read_start_kp, report_kp_basis


def _make_local_aeqd_crs(lat: float, lon: float) -> QgsCoordinateReferenceSystem:
    """Create a local azimuthal equidistant CRS (meters) centered on lat/lon (WGS84)."""

    proj = f"+proj=aeqd +lat_0={lat:.10f} +lon_0={lon:.10f} +datum=WGS84 +units=m +no_defs"

    if hasattr(QgsCoordinateReferenceSystem, 'fromProj4'):
        crs = QgsCoordinateReferenceSystem.fromProj4(proj)  # type: ignore[attr-defined]
        if crs.isValid():
            return crs

    crs = QgsCoordinateReferenceSystem()
    for method_name in ('createFromProj', 'createFromProj4'):
        if hasattr(crs, method_name):
            try:
                ok = getattr(crs, method_name)(proj)
                if ok and crs.isValid():
                    return crs
            except Exception:
                pass

    return QgsCoordinateReferenceSystem('EPSG:3857')

class PlaceSingleKpPointAlgorithm(SubseaCableAlgorithm):
    INPUT_LINE = 'INPUT_LINE'
    KP_VALUE = 'KP_VALUE'
    DCC_VALUE = 'DCC_VALUE'
    DCC_UNITS = 'DCC_UNITS'
    ADD_DCC_LINE = 'ADD_DCC_LINE'
    OUTPUT_DCC_LINE = 'OUTPUT_DCC_LINE'
    EXTEND_DCC_LINE = 'EXTEND_DCC_LINE'
    DCC_LINE_EXTEND = 'DCC_LINE_EXTEND'
    ADD_POINT_ON_RPL = 'ADD_POINT_ON_RPL'
    OUTPUT_POINT_ON_RPL = 'OUTPUT_POINT_ON_RPL'
    OUTPUT = 'OUTPUT'

    _DCC_UNIT_CHOICES = [
        'm',   # metres
        'km',  # kilometres
        'nm',  # nautical miles
        'ft',  # feet
    ]

    _DCC_UNIT_TO_METRES = {
        'm': 1.0,
        'km': 1000.0,
        'nm': 1852.0,
        'ft': 0.3048,
    }

    _SETTINGS_PREFIX = 'subsea_cable_tools/processing/placesinglekppoint/'

    def _skey(self, name: str) -> str:
        return f"{self._SETTINGS_PREFIX}{name}"

    def initAlgorithm(self, config=None):
        settings = QSettings()
        last_kp_val = settings.value(self._skey('kp_value_km'), 0.0, type=float)
        last_dcc_val = settings.value(self._skey('dcc_value'), 0.0, type=float)
        last_dcc_units = settings.value(self._skey('dcc_units_idx'), 0, type=int)
        last_add_dcc_line = settings.value(self._skey('add_dcc_line'), False, type=bool)
        last_extend_dcc_line = settings.value(self._skey('extend_dcc_line'), False, type=bool)
        last_dcc_line_extend = settings.value(self._skey('dcc_line_extend'), 0.0, type=float)
        last_add_point_on_rpl = settings.value(self._skey('add_point_on_rpl'), False, type=bool)

        if last_dcc_units < 0 or last_dcc_units >= len(self._DCC_UNIT_CHOICES):
            last_dcc_units = 0

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LINE,
                self.tr('Reference Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.KP_VALUE,
                self.tr('KP Value (Kilometers)'),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=0.0,
                defaultValue=last_kp_val
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DCC_VALUE,
                self.tr('Distance Cross Course (DCC)'),
                type=PROCESSING_NUMBER_DOUBLE,
                defaultValue=last_dcc_val
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.DCC_UNITS,
                self.tr('DCC Units'),
                options=self._DCC_UNIT_CHOICES,
                defaultValue=last_dcc_units
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output Point Layer')
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_DCC_LINE,
                self.tr('Add DCC Line Output'),
                defaultValue=last_add_dcc_line
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.EXTEND_DCC_LINE,
                self.tr('Extend DCC Line Past RPL'),
                defaultValue=last_extend_dcc_line
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DCC_LINE_EXTEND,
                self.tr('DCC Line Extension Length (same units as DCC)'),
                type=PROCESSING_NUMBER_DOUBLE,
                minValue=0.0,
                defaultValue=last_dcc_line_extend
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_DCC_LINE,
                self.tr('Output DCC Line Layer'),
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_POINT_ON_RPL,
                self.tr('Add Point on RPL Output'),
                defaultValue=last_add_point_on_rpl
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_POINT_ON_RPL,
                self.tr('Output Point on RPL Layer'),
                optional=True
            )
        )

        add_distance_mode_parameter(self)
        add_start_kp_parameter(self)

    def processAlgorithm(self, parameters, context, feedback):
        line_layer = self.parameterAsSource(parameters, self.INPUT_LINE, context)
        kp_val = self.parameterAsDouble(parameters, self.KP_VALUE, context)
        dcc_val = self.parameterAsDouble(parameters, self.DCC_VALUE, context)
        dcc_units_idx = self.parameterAsEnum(parameters, self.DCC_UNITS, context)
        add_dcc_line = self.parameterAsBool(parameters, self.ADD_DCC_LINE, context)
        extend_dcc_line = self.parameterAsBool(parameters, self.EXTEND_DCC_LINE, context)
        dcc_line_extend_val = self.parameterAsDouble(parameters, self.DCC_LINE_EXTEND, context)
        add_point_on_rpl = self.parameterAsBool(parameters, self.ADD_POINT_ON_RPL, context)

        # Persist last-used inputs for easier re-use.
        try:
            settings = QSettings()
            settings.setValue(self._skey('kp_value_km'), float(kp_val))
            settings.setValue(self._skey('dcc_value'), float(dcc_val))
            settings.setValue(self._skey('dcc_units_idx'), int(dcc_units_idx if dcc_units_idx is not None else 0))
            settings.setValue(self._skey('add_dcc_line'), bool(add_dcc_line))
            settings.setValue(self._skey('extend_dcc_line'), bool(extend_dcc_line))
            settings.setValue(self._skey('dcc_line_extend'), float(dcc_line_extend_val))
            settings.setValue(self._skey('add_point_on_rpl'), bool(add_point_on_rpl))
        except Exception:
            # Settings persistence is best-effort; do not fail the algorithm.
            pass

        if dcc_units_idx is None:
            dcc_units_idx = 0
        dcc_units_idx = int(dcc_units_idx)
        if dcc_units_idx < 0 or dcc_units_idx >= len(self._DCC_UNIT_CHOICES):
            dcc_units_idx = 0

        dcc_unit = self._DCC_UNIT_CHOICES[dcc_units_idx]
        dcc_m = float(dcc_val) * float(self._DCC_UNIT_TO_METRES.get(dcc_unit, 1.0))
        dcc_line_extend_m = float(dcc_line_extend_val) * float(self._DCC_UNIT_TO_METRES.get(dcc_unit, 1.0))

        if line_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LINE))

        # KP and DCC are measured in the route layer's CRS with the plugin's
        # KP method (kp_geo_utils.RouteFrame): they never depend on the
        # project CRS. Outputs stay in the route layer's CRS.
        source_crs = line_layer.sourceCrs()
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        transform_context = context.transformContext()
        to_wgs84 = QgsCoordinateTransform(source_crs, wgs84, transform_context)
        from_wgs84 = QgsCoordinateTransform(wgs84, source_crs, transform_context)

        output_fields = QgsFields()
        output_fields.append(QgsField('source_line', FIELD_TYPE_STRING))
        output_fields.append(QgsField('kp_value', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('dcc_value', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('dcc_unit', FIELD_TYPE_STRING))
        output_fields.append(QgsField('dcc_m', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('range_to_line_m', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('bearing_to_line_deg', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('bearing_from_line_deg', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('latitude', FIELD_TYPE_DOUBLE))
        output_fields.append(QgsField('longitude', FIELD_TYPE_DOUBLE))

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, output_fields, QgsWkbTypes.Point, line_layer.sourceCrs()
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        point_on_rpl_sink = None
        point_on_rpl_dest_id = None
        if add_point_on_rpl:
            (point_on_rpl_sink, point_on_rpl_dest_id) = self.parameterAsSink(
                parameters,
                self.OUTPUT_POINT_ON_RPL,
                context,
                QgsFields(output_fields),
                QgsWkbTypes.Point,
                line_layer.sourceCrs(),
            )

            if point_on_rpl_sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_POINT_ON_RPL))

        dcc_line_sink = None
        dcc_line_dest_id = None
        dcc_line_fields = None
        if add_dcc_line:
            dcc_line_fields = QgsFields()
            dcc_line_fields.append(QgsField('source_line', FIELD_TYPE_STRING))
            dcc_line_fields.append(QgsField('kp_value', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('dcc_value', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('dcc_unit', FIELD_TYPE_STRING))
            dcc_line_fields.append(QgsField('dcc_m', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('range_to_line_m', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('bearing_to_line_deg', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('bearing_from_line_deg', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('start_lat', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('start_lon', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('end_lat', FIELD_TYPE_DOUBLE))
            dcc_line_fields.append(QgsField('end_lon', FIELD_TYPE_DOUBLE))

            (dcc_line_sink, dcc_line_dest_id) = self.parameterAsSink(
                parameters,
                self.OUTPUT_DCC_LINE,
                context,
                dcc_line_fields,
                QgsWkbTypes.LineString,
                line_layer.sourceCrs(),
            )

            if dcc_line_sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_DCC_LINE))

        line_features = list(get_features_skip_invalid(line_layer))
        if not line_features:
            raise QgsProcessingException(self.tr("Input line layer has no features."))

        geometries = [f.geometry() for f in line_features if f.hasGeometry()]
        if not geometries:
            raise QgsProcessingException(self.tr("Input line layer has no geometries."))

        # Shared route builder (SeqNo/layer order, no noding or reversal).
        merged_geometry = ordered_route_geometry(line_features)

        if merged_geometry.isEmpty():
            raise QgsProcessingException(self.tr("Geometry is empty after merging features."))

        # KP distance is geodesic (WGS84) by default; Cartesian (grid) when chosen.
        distance_mode = read_distance_mode(self, parameters, context)
        try:
            distance_calculator = make_distance_area(
                source_crs, transform_context,
                mode=distance_mode, project=context.project(),
            )
        except ValueError as exc:
            raise QgsProcessingException(str(exc))

        start_kp = read_start_kp(self, parameters, context)
        route = build_route([merged_geometry], distance_calculator, start_kp)
        report_kp_basis(feedback, route, start_kp)
        if route.total_length_m <= 0.0:
            raise QgsProcessingException(self.tr("Line has no length."))

        # The position at the KP, on the KP path (start KP + chainage).
        base_pt = route.point_at_kp(kp_val)
        if base_pt is None:
            feedback.reportError(
                f"KP value {kp_val} is outside the line (KP {route.start_kp_km:.3f} to "
                f"{route.end_kp_km:.3f} km). Point not placed.")
            return {self.OUTPUT: dest_id}
        base_pt = QgsPointXY(base_pt)

        # Course at the KP: along the KP path just ahead (just behind at the
        # route end). At a vertex this is the outgoing leg.
        step_km = min(0.001, route.total_length_km / 2.0)
        if kp_val + step_km <= route.end_kp_km:
            course_from, course_to = base_pt, route.point_at_kp(kp_val + step_km)
        else:
            course_from, course_to = route.point_at_kp(max(route.start_kp_km, kp_val - step_km)), base_pt
        if course_from is None or course_to is None or (
                abs(course_to.x() - course_from.x()) <= 0.0 and abs(course_to.y() - course_from.y()) <= 0.0):
            feedback.reportError(self.tr(f"Could not place point at KP {kp_val}."), fatalError=True)
            return {self.OUTPUT: dest_id}
        course_from, course_to = QgsPointXY(course_from), QgsPointXY(course_to)

        # WGS84 geodesics for the offset (geodesic KP) and for true bearings.
        geodesic = make_distance_area(wgs84, transform_context)

        def _to_ll(point: QgsPointXY) -> QgsPointXY:
            return QgsPointXY(point) if source_crs == wgs84 else QgsPointXY(to_wgs84.transform(QgsPointXY(point)))

        def _from_ll(point: QgsPointXY) -> QgsPointXY:
            return QgsPointXY(point) if source_crs == wgs84 else QgsPointXY(from_wgs84.transform(QgsPointXY(point)))

        path_kind = kp_path_kind(distance_calculator)
        if path_kind == "geodesic":
            # The DCC runs along the geodesic perpendicular to the route at the
            # KP (starboard = course + 90 deg), so the offset point's nearest
            # point on the route is the KP position and its DCC is |DCC|.
            base_ll = _to_ll(base_pt)
            course = float(geodesic.bearing(_to_ll(course_from), _to_ll(course_to)))

            def _offset(metres: float, side: float) -> QgsPointXY:
                if metres <= 0.0:
                    return QgsPointXY(base_pt)
                return _from_ll(geodesic.computeSpheroidProject(base_ll, float(metres), course + side * math.pi / 2.0))

            def _measure(point_a: QgsPointXY, point_b: QgsPointXY) -> float:
                return float(geodesic.measureLine(_to_ll(point_a), _to_ll(point_b)))
        else:
            # Grid / planar KP: the KP path is straight in the grid (or in the
            # route CRS), so is the DCC, perpendicular to the route there.
            if path_kind == "grid":
                to_plane, from_plane = distance_calculator.to_grid, distance_calculator.from_grid
            else:
                def to_plane(point):
                    return QgsPointXY(point)

                def from_plane(point):
                    return QgsPointXY(point)
            plane_a, plane_b, plane_base = to_plane(course_from), to_plane(course_to), to_plane(base_pt)
            dx = float(plane_b.x() - plane_a.x())
            dy = float(plane_b.y() - plane_a.y())
            seg_len = math.hypot(dx, dy)
            if seg_len <= 0.0:
                raise QgsProcessingException(self.tr("Degenerate tangent segment encountered."))
            # Right normal (clockwise) for +ve DCC
            nx, ny = dy / seg_len, -dx / seg_len

            def _offset(metres: float, side: float) -> QgsPointXY:
                target = abs(float(metres))
                if target <= 0.0:
                    return QgsPointXY(base_pt)

                def candidate(scale: float) -> QgsPointXY:
                    return QgsPointXY(from_plane(QgsPointXY(
                        float(plane_base.x()) + side * float(scale) * nx,
                        float(plane_base.y()) + side * float(scale) * ny)))

                # Bracket, then bisect, so the measured distance is the target
                # whatever the plane's units.
                lo, hi = 0.0, 1.0
                dist_hi = float(distance_calculator.measureLine(base_pt, candidate(hi)))
                while dist_hi < target and hi < 1e9:
                    hi *= 2.0
                    dist_hi = float(distance_calculator.measureLine(base_pt, candidate(hi)))
                if dist_hi < target:
                    raise QgsProcessingException(self.tr("Failed to bracket the DCC distance."))
                for _ in range(60):
                    mid = (lo + hi) / 2.0
                    if float(distance_calculator.measureLine(base_pt, candidate(mid))) < target:
                        lo = mid
                    else:
                        hi = mid
                return candidate(hi)

            def _measure(point_a: QgsPointXY, point_b: QgsPointXY) -> float:
                return float(distance_calculator.measureLine(point_a, point_b))

        def _true_bearing_deg(point_a: QgsPointXY, point_b: QgsPointXY) -> float:
            """True bearing (deg clockwise from north) from point_a to point_b."""
            a_ll, b_ll = _to_ll(point_a), _to_ll(point_b)
            if abs(a_ll.x() - b_ll.x()) <= 0.0 and abs(a_ll.y() - b_ll.y()) <= 0.0:
                return 0.0
            return (math.degrees(float(geodesic.bearing(a_ll, b_ll))) + 360.0) % 360.0

        side = 1.0 if float(dcc_m) >= 0.0 else -1.0
        offset_pt = _offset(abs(float(dcc_m)), side)
        range_to_line_m = _measure(base_pt, offset_pt)
        bearing_from_line_deg = round(_true_bearing_deg(base_pt, offset_pt), 3)
        bearing_to_line_deg = round(_true_bearing_deg(offset_pt, base_pt), 3)

        point_geom = QgsGeometry.fromPointXY(QgsPointXY(float(offset_pt.x()), float(offset_pt.y())))

        transformed_geom = QgsGeometry(point_geom)
        transformed_geom.transform(to_wgs84)
        lon = float(transformed_geom.asPoint().x())
        lat = float(transformed_geom.asPoint().y())

        out_feat = QgsFeature(output_fields)
        out_feat.setGeometry(point_geom)
        out_feat.setAttribute('source_line', line_layer.sourceName())
        out_feat.setAttribute('kp_value', kp_val)
        out_feat.setAttribute('dcc_value', float(dcc_val))
        out_feat.setAttribute('dcc_unit', dcc_unit)
        out_feat.setAttribute('dcc_m', float(dcc_m))
        out_feat.setAttribute('range_to_line_m', range_to_line_m)
        out_feat.setAttribute('bearing_to_line_deg', bearing_to_line_deg)
        out_feat.setAttribute('bearing_from_line_deg', bearing_from_line_deg)
        out_feat.setAttribute('latitude', lat)
        out_feat.setAttribute('longitude', lon)

        sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
        feedback.pushInfo(self.tr(f"Placed 1 point at KP {kp_val} with DCC {dcc_val} {dcc_unit}."))

        # Optional: output the base point on the RPL at the same KP (DCC forced to 0).
        if add_point_on_rpl and point_on_rpl_sink is not None:
            base_geom_src = QgsGeometry.fromPointXY(QgsPointXY(float(base_pt.x()), float(base_pt.y())))

            base_geom_wgs = QgsGeometry(base_geom_src)
            base_geom_wgs.transform(to_wgs84)
            base_lon = float(base_geom_wgs.asPoint().x())
            base_lat = float(base_geom_wgs.asPoint().y())

            base_feat = QgsFeature(output_fields)
            base_feat.setGeometry(base_geom_src)
            base_feat.setAttribute('source_line', line_layer.sourceName())
            base_feat.setAttribute('kp_value', kp_val)
            base_feat.setAttribute('dcc_value', 0.0)
            base_feat.setAttribute('dcc_unit', dcc_unit)
            base_feat.setAttribute('dcc_m', 0.0)
            base_feat.setAttribute('range_to_line_m', 0.0)
            base_feat.setAttribute('bearing_to_line_deg', None)
            base_feat.setAttribute('bearing_from_line_deg', None)
            base_feat.setAttribute('latitude', base_lat)
            base_feat.setAttribute('longitude', base_lon)

            point_on_rpl_sink.addFeature(base_feat, QgsFeatureSink.FastInsert)

        # Optional: output the DCC line (from new point back to the route).
        if add_dcc_line and dcc_line_sink and dcc_line_fields is not None:
            out_pt_src = point_geom.asPoint()
            dcc_line_geom = QgsGeometry.fromPolylineXY([
                QgsPointXY(float(out_pt_src.x()), float(out_pt_src.y())),
                QgsPointXY(float(base_pt.x()), float(base_pt.y())),
            ])

            # Optionally extend the DCC line past the RPL base point (other side).
            if extend_dcc_line and dcc_line_extend_m > 0.0:
                if range_to_line_m <= 0.0:
                    feedback.pushInfo(self.tr("DCC is 0; cannot extend DCC line."))
                else:
                    # Extend from the base point to the other side of the route.
                    ext_pt = _offset(float(dcc_line_extend_m), -side)
                    dcc_line_geom = QgsGeometry.fromPolylineXY([
                        QgsPointXY(float(out_pt_src.x()), float(out_pt_src.y())),
                        QgsPointXY(float(base_pt.x()), float(base_pt.y())),
                        QgsPointXY(float(ext_pt.x()), float(ext_pt.y())),
                    ])

            line_feat = QgsFeature(dcc_line_fields)
            line_feat.setGeometry(dcc_line_geom)
            line_feat.setAttribute('source_line', line_layer.sourceName())
            line_feat.setAttribute('kp_value', kp_val)
            line_feat.setAttribute('dcc_value', float(dcc_val))
            line_feat.setAttribute('dcc_unit', dcc_unit)
            line_feat.setAttribute('dcc_m', float(dcc_m))
            line_feat.setAttribute('range_to_line_m', range_to_line_m)
            line_feat.setAttribute('bearing_to_line_deg', bearing_to_line_deg)
            line_feat.setAttribute('bearing_from_line_deg', bearing_from_line_deg)

            # Start/end lat/lon (EPSG:4326) for the line geometry
            try:
                pts = dcc_line_geom.asPolyline()
                if pts and len(pts) >= 2:
                    start_src = QgsGeometry.fromPointXY(QgsPointXY(pts[0]))
                    end_src = QgsGeometry.fromPointXY(QgsPointXY(pts[-1]))
                    start_wgs = QgsGeometry(start_src)
                    end_wgs = QgsGeometry(end_src)
                    start_wgs.transform(to_wgs84)
                    end_wgs.transform(to_wgs84)
                    line_feat.setAttribute('start_lat', float(start_wgs.asPoint().y()))
                    line_feat.setAttribute('start_lon', float(start_wgs.asPoint().x()))
                    line_feat.setAttribute('end_lat', float(end_wgs.asPoint().y()))
                    line_feat.setAttribute('end_lon', float(end_wgs.asPoint().x()))
            except Exception:
                pass
            dcc_line_sink.addFeature(line_feat, QgsFeatureSink.FastInsert)

        results = {self.OUTPUT: dest_id}
        if add_dcc_line:
            results[self.OUTPUT_DCC_LINE] = dcc_line_dest_id
        if add_point_on_rpl:
            results[self.OUTPUT_POINT_ON_RPL] = point_on_rpl_dest_id
        return results

    def shortHelpString(self):
        return self.tr("""
This tool places a single point at a specified Kilometer Point (KP) along a line layer.

You can optionally apply a Distance Cross Course (DCC) offset to place the point left/right of the route.
Positive DCC is to the right side of the line (increasing KP direction); negative DCC is to the left.

**Instructions:**

1.  **Input Line Layer:** Choose the line layer on which you want to place the KP point. The tool will treat all lines in this layer as a single, continuous route (in SeqNo order when every feature has one, else layer order).
2.  **KP Value (Kilometers):** Enter the exact KP value where you want the point to be placed.
3.  **Distance Cross Course (DCC):** Optional lateral offset from the route centerline.
4.  **DCC Units:** Select the units for the DCC value (default is metres).
5.  **Run:** Execute the tool. The output will be a new point layer containing the single point.

**Optional Output:** Enable **Add DCC Line Output** to also create a line layer connecting the output point back to its reference location on the route.
Enable **Extend DCC Line Past RPL** and set **DCC Line Extension Length** to extend that line beyond the route (past the base KP point) by the given amount.
Enable **Add Point on RPL Output** to also create a point layer containing the on-route KP location (DCC forced to 0).

**Note:** KP and DCC follow the plugin's KP method in the route layer's own CRS, whatever the project CRS. The point at the KP lies on the route's KP path (between vertices along the geodesic, or the straight grid line for grid KP). The DCC offset is perpendicular to the route at that point: along the geodesic for geodesic KP, straight in the grid for grid KP. Bearings are true bearings (degrees clockwise from north).
""") + "\n\n" + self.tr(START_KP_HELP)

    def name(self):
        return 'placesinglekppoint'

    def displayName(self):
        return self.tr('Place Single KP Point')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def createInstance(self):
        return PlaceSingleKpPointAlgorithm()

    def tr(self, string):
        return QCoreApplication.translate("PlaceSingleKpPointAlgorithm", string)
