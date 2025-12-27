# place_single_kp_point_algorithm.py
# -*- coding: utf-8 -*-
"""
PlaceSingleKpPointAlgorithm
This tool places a single KP point along a route.
"""

from qgis.PyQt.QtCore import QCoreApplication, QVariant, QSettings
from qgis.core import (QgsProcessing,
                       QgsFeatureSink,
                       QgsProcessingAlgorithm,
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
                       QgsDistanceArea,
                       QgsProcessingException,
                       QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform)


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

class PlaceSingleKpPointAlgorithm(QgsProcessingAlgorithm):
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
                self.tr('Input Line Layer'),
                [QgsProcessing.TypeVectorLine]
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.KP_VALUE,
                self.tr('KP Value (Kilometers)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=last_kp_val
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.DCC_VALUE,
                self.tr('Distance Cross Course (DCC)'),
                type=QgsProcessingParameterNumber.Double,
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
                type=QgsProcessingParameterNumber.Double,
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

        source_crs = line_layer.sourceCrs()
        project_crs = context.project().crs()
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        to_wgs84 = QgsCoordinateTransform(source_crs, wgs84, context.project())

        src_to_project = QgsCoordinateTransform(source_crs, project_crs, context.project())
        project_to_src = QgsCoordinateTransform(project_crs, source_crs, context.project())

        output_fields = QgsFields()
        output_fields.append(QgsField('source_line', QVariant.String))
        output_fields.append(QgsField('kp_value', QVariant.Double))
        output_fields.append(QgsField('dcc_value', QVariant.Double))
        output_fields.append(QgsField('dcc_unit', QVariant.String))
        output_fields.append(QgsField('dcc_m', QVariant.Double))
        output_fields.append(QgsField('range_to_line_m', QVariant.Double))
        output_fields.append(QgsField('bearing_to_line_deg', QVariant.Double))
        output_fields.append(QgsField('bearing_from_line_deg', QVariant.Double))
        output_fields.append(QgsField('latitude', QVariant.Double))
        output_fields.append(QgsField('longitude', QVariant.Double))

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
            dcc_line_fields.append(QgsField('source_line', QVariant.String))
            dcc_line_fields.append(QgsField('kp_value', QVariant.Double))
            dcc_line_fields.append(QgsField('dcc_value', QVariant.Double))
            dcc_line_fields.append(QgsField('dcc_unit', QVariant.String))
            dcc_line_fields.append(QgsField('dcc_m', QVariant.Double))
            dcc_line_fields.append(QgsField('range_to_line_m', QVariant.Double))
            dcc_line_fields.append(QgsField('bearing_to_line_deg', QVariant.Double))
            dcc_line_fields.append(QgsField('bearing_from_line_deg', QVariant.Double))
            dcc_line_fields.append(QgsField('start_lat', QVariant.Double))
            dcc_line_fields.append(QgsField('start_lon', QVariant.Double))
            dcc_line_fields.append(QgsField('end_lat', QVariant.Double))
            dcc_line_fields.append(QgsField('end_lon', QVariant.Double))

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

        line_features = list(line_layer.getFeatures())
        if not line_features:
            raise QgsProcessingException(self.tr("Input line layer has no features."))

        geometries = [f.geometry() for f in line_features if f.hasGeometry()]
        if not geometries:
            raise QgsProcessingException(self.tr("Input line layer has no geometries."))

        try:
            combined_geom = QgsGeometry.unaryUnion(geometries)
        except Exception:
            combined_geom = QgsGeometry.collectGeometry(geometries)

        if combined_geom and combined_geom.type() != QgsWkbTypes.LineGeometry:
            try:
                combined_geom = combined_geom.convertToType(QgsWkbTypes.LineGeometry, True)
            except Exception:
                pass

        merged_geometry = combined_geom
        if merged_geometry is not None:
            if hasattr(merged_geometry, 'mergeLines'):
                try:
                    merged_geometry = merged_geometry.mergeLines()
                except Exception:
                    merged_geometry = combined_geom
            elif hasattr(merged_geometry, 'lineMerge'):
                try:
                    merged_geometry = merged_geometry.lineMerge()
                except Exception:
                    merged_geometry = combined_geom
        
        if merged_geometry.isEmpty():
            raise QgsProcessingException(self.tr("Geometry is empty after merging features."))

        # Work in the project CRS to match the KP mouse tool's behaviour.
        merged_project = QgsGeometry(merged_geometry)
        merged_project.transform(src_to_project)
        if merged_project.isEmpty():
            raise QgsProcessingException(self.tr("Geometry became empty after transforming to project CRS."))

        # KP distance is measured geodesically (ellipsoidal), matching the KP mouse tool.
        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(project_crs, context.transformContext())
        ellipsoid = context.project().ellipsoid()
        if not ellipsoid:
            ellipsoid = 'WGS84'
        distance_calculator.setEllipsoid(ellipsoid)
        if hasattr(distance_calculator, "setEllipsoidalMode"):
            distance_calculator.setEllipsoidalMode(True)

        total_length_m = float(distance_calculator.measureLength(merged_project))
        if total_length_m <= 0.0:
            raise QgsProcessingException(self.tr("Line has no length."))

        kp_dist_m = kp_val * 1000
        if kp_dist_m > total_length_m:
            feedback.reportError(f"KP value {kp_val} is beyond the line's total length of {total_length_m/1000:.3f} km. Point not placed.")
            return {self.OUTPUT: dest_id}

        # Locate the KP point and segment tangent in the project CRS.
        line_parts = merged_project.asMultiPolyline() if merged_project.isMultipart() else [merged_project.asPolyline()]
        if not line_parts or all(len(p) < 2 for p in line_parts):
            raise QgsProcessingException(self.tr("Line geometry has insufficient vertices."))

        cumulative_length = 0.0
        point_found = False
        base_x = None
        base_y = None
        seg_p1_proj = None
        seg_p2_proj = None

        for part in line_parts:
            if len(part) < 2:
                continue
            for i in range(len(part) - 1):
                p1, p2 = part[i], part[i + 1]

                # Geodesic segment length (meters)
                segment_length = float(distance_calculator.measureLine(p1, p2))
                if segment_length <= 0.0:
                    continue

                is_last_segment = (i == len(part) - 2)
                within = (cumulative_length <= kp_dist_m < cumulative_length + segment_length)
                at_end = is_last_segment and abs(kp_dist_m - (cumulative_length + segment_length)) <= 1e-6

                if within or at_end:
                    dist_into_segment = kp_dist_m - cumulative_length
                    if at_end:
                        dist_into_segment = segment_length
                    ratio = dist_into_segment / segment_length

                    # Interpolate along the actual stored segment in the source CRS.
                    # This matches how KP is represented on the polyline itself.
                    base_x = float(p1.x()) + ratio * float(p2.x() - p1.x())
                    base_y = float(p1.y()) + ratio * float(p2.y() - p1.y())
                    seg_p1_proj = QgsPointXY(p1)
                    seg_p2_proj = QgsPointXY(p2)
                    point_found = True
                    break

                cumulative_length += segment_length

            if point_found:
                break

        if not point_found:
            feedback.reportError(self.tr(f"Could not place point at KP {kp_val}."), fatalError=True)
            return {self.OUTPUT: dest_id}

        if seg_p1_proj is None or seg_p2_proj is None or base_x is None or base_y is None:
            raise QgsProcessingException(self.tr("Internal error locating KP segment."))

        base_pt_proj = QgsPointXY(float(base_x), float(base_y))

        def _solve_offset_perp_in_project(
            base_pt: QgsPointXY,
            seg_p1: QgsPointXY,
            seg_p2: QgsPointXY,
            signed_target_m: float,
        ):
            """Return (offset_pt, abs_distance_m) in project CRS.

            Constructs an offset point in the plane of the project CRS, perpendicular to the
            segment direction, and solves for a scale so that geodesic distance(base, offset)
            matches the requested magnitude in meters.
            """

            import math

            dx = float(seg_p2.x() - seg_p1.x())
            dy = float(seg_p2.y() - seg_p1.y())
            seg_len = (dx * dx + dy * dy) ** 0.5
            if seg_len <= 0.0:
                raise QgsProcessingException(self.tr("Degenerate tangent segment encountered."))

            # Right normal (clockwise) for +ve DCC
            nx = dy / seg_len
            ny = -dx / seg_len

            target = abs(float(signed_target_m))
            if target <= 0.0:
                return QgsPointXY(float(base_pt.x()), float(base_pt.y())), 0.0

            sign = 1.0 if float(signed_target_m) >= 0.0 else -1.0

            def candidate(scale: float) -> QgsPointXY:
                return QgsPointXY(
                    float(base_pt.x()) + sign * float(scale) * nx,
                    float(base_pt.y()) + sign * float(scale) * ny,
                )

            # Bracket the solution in CRS units.
            lo = 0.0
            hi = 1.0
            dist_hi = float(distance_calculator.measureLine(base_pt, candidate(hi)))
            while dist_hi < target and hi < 1e9:
                hi *= 2.0
                dist_hi = float(distance_calculator.measureLine(base_pt, candidate(hi)))

            if dist_hi < target:
                # Should never happen unless the distance calculator failed.
                raise QgsProcessingException(self.tr("Failed to bracket DCC distance in project CRS."))

            # Binary search for scale giving target distance.
            for _ in range(40):
                mid = (lo + hi) / 2.0
                dist_mid = float(distance_calculator.measureLine(base_pt, candidate(mid)))
                if dist_mid < target:
                    lo = mid
                else:
                    hi = mid

            offset_pt = candidate(hi)
            dist = float(distance_calculator.measureLine(base_pt, offset_pt))
            return offset_pt, dist

        def _solve_along_dir_in_project(base_pt: QgsPointXY, dir_x: float, dir_y: float, target_m: float) -> QgsPointXY:
            """Move from base_pt along (dir_x, dir_y) in project CRS such that measureLine(base, moved) == target_m."""
            import math

            if target_m <= 0.0:
                return QgsPointXY(float(base_pt.x()), float(base_pt.y()))

            dlen = (float(dir_x) * float(dir_x) + float(dir_y) * float(dir_y)) ** 0.5
            if dlen <= 0.0:
                raise QgsProcessingException(self.tr("Cannot extend DCC line: zero direction vector."))

            ux = float(dir_x) / dlen
            uy = float(dir_y) / dlen

            def candidate(scale: float) -> QgsPointXY:
                return QgsPointXY(
                    float(base_pt.x()) + float(scale) * ux,
                    float(base_pt.y()) + float(scale) * uy,
                )

            lo = 0.0
            hi = 1.0
            dist_hi = float(distance_calculator.measureLine(base_pt, candidate(hi)))
            while dist_hi < float(target_m) and hi < 1e9:
                hi *= 2.0
                dist_hi = float(distance_calculator.measureLine(base_pt, candidate(hi)))

            for _ in range(40):
                mid = (lo + hi) / 2.0
                dist_mid = float(distance_calculator.measureLine(base_pt, candidate(mid)))
                if dist_mid < float(target_m):
                    lo = mid
                else:
                    hi = mid

            return candidate(hi)

        # Apply DCC offset in project CRS, tuned so geodesic distance matches the requested value.
        offset_pt_proj, measured_range_m = _solve_offset_perp_in_project(
            base_pt_proj,
            seg_p1_proj,
            seg_p2_proj,
            float(dcc_m),
        )

        # Range/bearings in project CRS, matching the KP mouse tool conventions.
        range_to_line_m = float(measured_range_m)

        def _bearing_deg(pointA: QgsPointXY, pointB: QgsPointXY) -> float:
            dx_b = float(pointB.x() - pointA.x())
            dy_b = float(pointB.y() - pointA.y())
            # Clockwise from north
            import math
            angle_rad = math.atan2(dx_b, dy_b)
            angle_deg = math.degrees(angle_rad)
            return (angle_deg + 360.0) % 360.0

        bearing_from_line_deg = round(
            _bearing_deg(QgsPointXY(float(base_pt_proj.x()), float(base_pt_proj.y())), QgsPointXY(float(offset_pt_proj.x()), float(offset_pt_proj.y()))),
            3,
        )
        bearing_to_line_deg = round(
            _bearing_deg(QgsPointXY(float(offset_pt_proj.x()), float(offset_pt_proj.y())), QgsPointXY(float(base_pt_proj.x()), float(base_pt_proj.y()))),
            3,
        )

        # Transform base/offset back to the input layer CRS for output.
        base_pt_src = project_to_src.transform(base_pt_proj)
        offset_pt_src = project_to_src.transform(offset_pt_proj)

        point_geom = QgsGeometry.fromPointXY(QgsPointXY(float(offset_pt_src.x()), float(offset_pt_src.y())))

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
            base_geom_src = QgsGeometry.fromPointXY(QgsPointXY(float(base_pt_src.x()), float(base_pt_src.y())))

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
                QgsPointXY(float(base_pt_src.x()), float(base_pt_src.y())),
            ])

            # Optionally extend the DCC line past the RPL base point (other side).
            if extend_dcc_line and dcc_line_extend_m > 0.0:
                # Direction from base -> offset in project CRS.
                dir_x = float(offset_pt_proj.x() - base_pt_proj.x())
                dir_y = float(offset_pt_proj.y() - base_pt_proj.y())
                if abs(dir_x) <= 0.0 and abs(dir_y) <= 0.0:
                    feedback.pushInfo(self.tr("DCC is 0; cannot extend DCC line."))
                else:
                    # Extend from base in the opposite direction (past the RPL).
                    ext_pt_proj = _solve_along_dir_in_project(
                        base_pt_proj,
                        -dir_x,
                        -dir_y,
                        float(dcc_line_extend_m),
                    )
                    ext_pt_src = project_to_src.transform(ext_pt_proj)
                    dcc_line_geom = QgsGeometry.fromPolylineXY([
                        QgsPointXY(float(out_pt_src.x()), float(out_pt_src.y())),
                        QgsPointXY(float(base_pt_src.x()), float(base_pt_src.y())),
                        QgsPointXY(float(ext_pt_src.x()), float(ext_pt_src.y())),
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

1.  **Input Line Layer:** Choose the line layer on which you want to place the KP point. The tool will treat all lines in this layer as a single, continuous route.
2.  **KP Value (Kilometers):** Enter the exact KP value where you want the point to be placed.
3.  **Distance Cross Course (DCC):** Optional lateral offset from the route centerline.
4.  **DCC Units:** Select the units for the DCC value (default is metres).
5.  **Run:** Execute the tool. The output will be a new point layer containing the single point.

**Optional Output:** Enable **Add DCC Line Output** to also create a line layer connecting the output point back to its reference location on the route.
Enable **Extend DCC Line Past RPL** and set **DCC Line Extension Length** to extend that line beyond the route (past the base KP point) by the given amount.
Enable **Add Point on RPL Output** to also create a point layer containing the on-route KP location (DCC forced to 0).

**Note:** KP/DCC are calculated consistently with project CRS measurements.
""")

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
