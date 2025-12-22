# identify_rpl_crossing_points_algorithm.py
# -*- coding: utf-8 -*-
"""IdentifyRPLCrossingPointsAlgorithm

Creates a point layer of crossings between an RPL line layer and one-or-more "Assets" line layers.

Output includes:
- KP (km) along the RPL (supports multi-feature RPL layers)
- Lat/Lon (EPSG:4326) of each crossing point
- Relative crossing angle (degrees, 0-180)
- References to input layer names
- Attributes of the crossed asset feature (as separate columns)

"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingLayerPostProcessorInterface,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsProcessingParameterMultipleLayers,
    QgsProject,
    QgsSpatialIndex,
    QgsWkbTypes,
)


@dataclass(frozen=True)
class _RplGeomInfo:
    fid: int
    geom: QgsGeometry
    cumulative_base_m: float


def _as_parts(line_geometry: QgsGeometry):
    if line_geometry is None or line_geometry.isEmpty():
        return []

    if line_geometry.isMultipart():
        try:
            return list(line_geometry.asMultiPolyline())
        except Exception:
            return []

    try:
        return [line_geometry.asPolyline()]
    except Exception:
        return []


def _kp_m_on_geom(
    point_on_or_near_geom: QgsPointXY,
    geom: QgsGeometry,
    cumulative_base_m: float,
    distance: QgsDistanceArea,
    tolerance_m: float = 0.5,
) -> float:
    """Returns KP in meters to a point on/near a specific geometry, including cumulative_base_m."""

    target_pt_xy = QgsPointXY(point_on_or_near_geom)
    cumulative_length = float(cumulative_base_m)

    parts = _as_parts(geom)
    for part in parts:
        for i in range(len(part) - 1):
            v1 = QgsPointXY(part[i])
            v2 = QgsPointXY(part[i + 1])
            seg_len = float(distance.measureLine(v1, v2))
            if seg_len <= 0.0:
                continue

            segment_geom = QgsGeometry.fromPolylineXY([v1, v2])
            nearest_on_segment = segment_geom.nearestPoint(QgsGeometry.fromPointXY(target_pt_xy))
            if not nearest_on_segment.isEmpty():
                nearest_pt = nearest_on_segment.asPoint()
                dist_to_nearest = float(distance.measureLine(target_pt_xy, QgsPointXY(nearest_pt)))
                if dist_to_nearest <= tolerance_m:
                    dist_along_segment = float(distance.measureLine(v1, QgsPointXY(nearest_pt)))
                    return cumulative_length + dist_along_segment

            cumulative_length += seg_len

    # Fallback: snap to nearest point on geom, then retry with a slightly larger tolerance.
    snapped = geom.nearestPoint(QgsGeometry.fromPointXY(target_pt_xy))
    if not snapped.isEmpty():
        try:
            snapped_xy = QgsPointXY(snapped.asPoint())
            if tolerance_m < 2.0:
                return _kp_m_on_geom(snapped_xy, geom, cumulative_base_m, distance, tolerance_m=2.0)
        except Exception:
            pass

    return float(cumulative_base_m)


def _distance_point_to_segment_sq(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Squared distance from point P to segment AB in 2D (planar)."""

    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    ab_len_sq = abx * abx + aby * aby
    if ab_len_sq <= 0.0:
        dx = px - ax
        dy = py - ay
        return dx * dx + dy * dy

    t = (apx * abx + apy * aby) / ab_len_sq
    t = max(0.0, min(1.0, t))
    cx = ax + t * abx
    cy = ay + t * aby
    dx = px - cx
    dy = py - cy
    return dx * dx + dy * dy


def _direction_deg_of_nearest_segment(line_geom: QgsGeometry, point_xy: QgsPointXY) -> Optional[float]:
    """Returns direction (deg 0-360) of nearest segment to point_xy, in the geometry's CRS units."""

    parts = _as_parts(line_geom)
    if not parts:
        return None

    px = float(point_xy.x())
    py = float(point_xy.y())

    best = None  # (dist_sq, dx, dy)
    for part in parts:
        if len(part) < 2:
            continue
        for i in range(len(part) - 1):
            p1 = part[i]
            p2 = part[i + 1]
            ax = float(p1.x())
            ay = float(p1.y())
            bx = float(p2.x())
            by = float(p2.y())
            dist_sq = _distance_point_to_segment_sq(px, py, ax, ay, bx, by)
            dx = bx - ax
            dy = by - ay
            if best is None or dist_sq < best[0]:
                best = (dist_sq, dx, dy)

    if best is None:
        return None

    _, dx, dy = best
    if dx == 0.0 and dy == 0.0:
        return None

    ang = math.degrees(math.atan2(dy, dx))
    if ang < 0.0:
        ang += 360.0
    return ang


def _relative_angle_deg(a1: Optional[float], a2: Optional[float]) -> Optional[float]:
    if a1 is None or a2 is None:
        return None
    diff = abs(a1 - a2)
    if diff > 180.0:
        diff = 360.0 - diff
    return diff


def _safe_field_base_name(name: str) -> str:
    name = (name or '').strip()
    if not name:
        return 'field'
    # QGIS providers differ, but keep it conservative-ish.
    return name[:60]


def _unique_field_name(existing: set[str], base: str) -> str:
    base = _safe_field_base_name(base)
    if base not in existing:
        existing.add(base)
        return base

    i = 2
    while True:
        candidate = _safe_field_base_name(f"{base}_{i}")
        if candidate not in existing:
            existing.add(candidate)
            return candidate
        i += 1


def _asset_field_definitions(asset_layers) -> Tuple[List[Tuple[str, str]], List[QgsField]]:
    """Return a mapping and output fields for asset attributes.

    Returns:
        - mapping: list of tuples (output_field_name, source_field_name)
        - fields: list of QgsField for output
    """

    seen_out_names: set[str] = set()
    # Reserve our fixed output fields.
    for reserved in ['rpl_layer', 'asset_layer', 'rpl_fid', 'asset_fid', 'kp', 'lat', 'lon', 'cross_ang']:
        seen_out_names.add(reserved)

    # Track source field types to preserve types when consistent; otherwise fall back to string.
    # key: source field name -> QgsField
    chosen: Dict[str, QgsField] = {}
    conflicts: set[str] = set()

    for layer in asset_layers:
        for fld in layer.fields():
            src_name = fld.name()
            if src_name in chosen and src_name not in conflicts:
                if chosen[src_name].type() != fld.type():
                    conflicts.add(src_name)
            else:
                chosen.setdefault(src_name, QgsField(src_name, fld.type(), fld.typeName(), fld.length(), fld.precision()))

    # Build output fields and mapping. Prefix with asset_ to avoid collisions.
    mapping: List[Tuple[str, str]] = []
    out_fields: List[QgsField] = []
    for src_name in sorted(chosen.keys(), key=lambda s: (s or '').lower()):
        out_name = _unique_field_name(seen_out_names, f"asset_{src_name}")
        if src_name in conflicts:
            out_fields.append(QgsField(out_name, QVariant.String))
        else:
            f = chosen[src_name]
            out_fields.append(QgsField(out_name, f.type(), f.typeName(), f.length(), f.precision()))
        mapping.append((out_name, src_name))

    return mapping, out_fields


def _make_local_aeqd_crs(lat: float, lon: float) -> QgsCoordinateReferenceSystem:
    """Create a local azimuthal equidistant CRS (meters) centered on lat/lon (WGS84)."""

    proj = f"+proj=aeqd +lat_0={lat:.10f} +lon_0={lon:.10f} +datum=WGS84 +units=m +no_defs"

    # Try a few creation methods to be compatible across QGIS versions.
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

    # Fallback (less accurate globally, but meters-based)
    return QgsCoordinateReferenceSystem('EPSG:3857')


def _nearest_along_m_planar(line_geom: QgsGeometry, point_xy: QgsPointXY) -> Optional[float]:
    """Planar linear-referencing: distance (m) along line to closest point."""

    parts = _as_parts(line_geom)
    if not parts:
        return None

    px = float(point_xy.x())
    py = float(point_xy.y())
    cumulative = 0.0
    best_dist_sq = None
    best_along = None

    for part in parts:
        if len(part) < 2:
            continue
        for i in range(len(part) - 1):
            p1 = part[i]
            p2 = part[i + 1]
            ax = float(p1.x())
            ay = float(p1.y())
            bx = float(p2.x())
            by = float(p2.y())
            dx = bx - ax
            dy = by - ay
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0.0:
                continue

            t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
            t = max(0.0, min(1.0, t))
            cx = ax + t * dx
            cy = ay + t * dy

            ddx = px - cx
            ddy = py - cy
            dist_sq = ddx * ddx + ddy * ddy
            seg_len = math.sqrt(seg_len_sq)
            along = cumulative + t * seg_len

            if best_dist_sq is None or dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_along = along

            cumulative += seg_len

    return best_along


def _extract_subline_planar(line_geom: QgsGeometry, start_m: float, end_m: float) -> Optional[QgsGeometry]:
    """Extract a sub-line in planar meters from start_m to end_m along a (multi)polyline."""

    try:
        start_m = float(start_m)
        end_m = float(end_m)
    except Exception:
        return None

    if start_m > end_m:
        start_m, end_m = end_m, start_m

    if end_m <= 0.0:
        return None

    start_m = max(0.0, start_m)

    parts = _as_parts(line_geom)
    if not parts:
        return None

    total_len = 0.0
    for part in parts:
        for i in range(len(part) - 1):
            dx = float(part[i + 1].x() - part[i].x())
            dy = float(part[i + 1].y() - part[i].y())
            total_len += math.hypot(dx, dy)

    if total_len <= 0.0:
        return None

    if start_m >= total_len:
        return None

    end_m = min(end_m, total_len)

    segment_points: List[Any] = []
    cumulative = 0.0
    started = False

    for part in parts:
        if len(part) < 2:
            continue
        for i in range(len(part) - 1):
            p1 = part[i]
            p2 = part[i + 1]
            dx = float(p2.x() - p1.x())
            dy = float(p2.y() - p1.y())
            seg_len = math.hypot(dx, dy)
            if seg_len <= 0.0:
                continue

            next_cum = cumulative + seg_len

            if not started and next_cum >= start_m:
                ratio = (start_m - cumulative) / seg_len
                x = float(p1.x()) + ratio * dx
                y = float(p1.y()) + ratio * dy
                segment_points.append(QgsPointXY(x, y))
                started = True

            if started:
                if next_cum <= end_m:
                    segment_points.append(QgsPointXY(p2))
                else:
                    ratio = (end_m - cumulative) / seg_len
                    x = float(p1.x()) + ratio * dx
                    y = float(p1.y()) + ratio * dy
                    segment_points.append(QgsPointXY(x, y))
                    try:
                        return QgsGeometry.fromPolylineXY(segment_points)
                    except Exception:
                        return None

            cumulative = next_cum

    if not started or len(segment_points) < 2:
        return None

    try:
        return QgsGeometry.fromPolylineXY(segment_points)
    except Exception:
        return None


def _extract_points(geom: QgsGeometry) -> List[QgsPointXY]:
    """Extract point(s) from a geometry returned by intersection."""

    if geom is None or geom.isEmpty():
        return []

    gtype = QgsWkbTypes.geometryType(geom.wkbType())
    if gtype == QgsWkbTypes.PointGeometry:
        if QgsWkbTypes.isMultiType(geom.wkbType()):
            try:
                return [QgsPointXY(p) for p in geom.asMultiPoint()]
            except Exception:
                return []
        try:
            return [QgsPointXY(geom.asPoint())]
        except Exception:
            return []

    # Geometry collections can include points.
    if QgsWkbTypes.isGeometryCollection(geom.wkbType()):
        points: List[QgsPointXY] = []
        try:
            for part in geom.asGeometryCollection():
                points.extend(_extract_points(part))
        except Exception:
            return []
        return points

    # For line/polygon intersections (overlaps), treat as "not a crossing".
    return []


class IdentifyRPLCrossingPointsAlgorithm(QgsProcessingAlgorithm):
    INPUT_RPL = 'INPUT_RPL'
    INPUT_ASSETS = 'INPUT_ASSETS'
    OUTPUT = 'OUTPUT'

    BUFFER_HALF_LEN_M = 'BUFFER_HALF_LEN_M'
    BUFFER_DIST_M = 'BUFFER_DIST_M'
    OUTPUT_BUFFER = 'OUTPUT_BUFFER'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_RPL,
                self.tr('Input RPL Line Layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_ASSETS,
                self.tr('Assets Line Layer(s)'),
                layerType=QgsProcessing.TypeVectorLine,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Crossings Listing Output'),
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUFFER_HALF_LEN_M,
                self.tr('Buffer segment half-length along asset (m) (optional)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUFFER_DIST_M,
                self.tr('Buffer distance (m) (optional)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_BUFFER,
                self.tr('Crossing Buffers Output (optional)'),
                optional=True,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        rpl_source = self.parameterAsSource(parameters, self.INPUT_RPL, context)
        if rpl_source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_RPL))

        rpl_layer = self.parameterAsVectorLayer(parameters, self.INPUT_RPL, context)
        rpl_layer_name = rpl_layer.name() if rpl_layer is not None else (rpl_source.sourceName() or 'RPL')

        asset_layers = self.parameterAsLayerList(parameters, self.INPUT_ASSETS, context) or []
        asset_layers = [lyr for lyr in asset_layers if getattr(lyr, 'isValid', lambda: False)()]
        if not asset_layers:
            raise QgsProcessingException(self.tr('No asset line layers were provided.'))

        rpl_crs = rpl_source.sourceCrs()

        # Distance calculator for KP measurements (geodetic via project ellipsoid)
        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(rpl_crs, context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

        # Pre-load RPL geometries in feature order and compute cumulative bases
        rpl_infos: List[_RplGeomInfo] = []
        cumulative_base_m = 0.0
        for f in rpl_source.getFeatures():
            if feedback.isCanceled():
                break
            if not f.hasGeometry():
                continue
            g = QgsGeometry(f.geometry())
            if g.isEmpty():
                continue
            rpl_infos.append(_RplGeomInfo(fid=f.id(), geom=g, cumulative_base_m=cumulative_base_m))
            cumulative_base_m += float(distance_calculator.measureLength(g))

        if not rpl_infos:
            return {self.OUTPUT: None}

        # Asset attribute fields (union across selected asset layers)
        asset_attr_mapping, asset_attr_fields = _asset_field_definitions(asset_layers)

        # Output fields
        fields = QgsFields()
        fields.append(QgsField('rpl_layer', QVariant.String))
        fields.append(QgsField('asset_layer', QVariant.String))
        fields.append(QgsField('rpl_fid', QVariant.LongLong))
        fields.append(QgsField('asset_fid', QVariant.LongLong))
        fields.append(QgsField('kp', QVariant.Double))
        fields.append(QgsField('lat', QVariant.Double))
        fields.append(QgsField('lon', QVariant.Double))
        fields.append(QgsField('cross_ang', QVariant.Double))
        for f in asset_attr_fields:
            fields.append(f)

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.Point,
            rpl_crs,
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        # Prepare transform to WGS84 for lat/lon fields
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        to_wgs84 = QgsCoordinateTransform(rpl_crs, wgs84, context.transformContext())

        # Optional buffer output (value-driven)
        buffer_half_len_m = float(self.parameterAsDouble(parameters, self.BUFFER_HALF_LEN_M, context) or 0.0)
        buffer_dist_m = float(self.parameterAsDouble(parameters, self.BUFFER_DIST_M, context) or 0.0)

        buffer_sink = None
        buffer_dest_id = None
        buffer_fields = None
        if buffer_dist_m > 0.0:
            buffer_fields = QgsFields()
            buffer_fields.append(QgsField('rpl_layer', QVariant.String))
            buffer_fields.append(QgsField('asset_layer', QVariant.String))
            buffer_fields.append(QgsField('rpl_fid', QVariant.LongLong))
            buffer_fields.append(QgsField('asset_fid', QVariant.LongLong))
            buffer_fields.append(QgsField('kp', QVariant.Double))
            buffer_fields.append(QgsField('cross_ang', QVariant.Double))
            buffer_fields.append(QgsField('half_len_m', QVariant.Double))
            buffer_fields.append(QgsField('buf_dist_m', QVariant.Double))
            for f in asset_attr_fields:
                buffer_fields.append(f)

            (buffer_sink, buffer_dest_id) = self.parameterAsSink(
                parameters,
                self.OUTPUT_BUFFER,
                context,
                buffer_fields,
                QgsWkbTypes.Polygon,
                rpl_crs,
            )

            if buffer_sink is None:
                raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT_BUFFER))

        # Prepare per-asset layer spatial index and transforms
        asset_prepared = []
        for asset_layer in asset_layers:
            asset_crs = asset_layer.crs()
            to_asset = QgsCoordinateTransform(rpl_crs, asset_crs, context.transformContext())
            to_rpl = QgsCoordinateTransform(asset_crs, rpl_crs, context.transformContext())
            index = QgsSpatialIndex(asset_layer.getFeatures())
            asset_prepared.append((asset_layer, index, to_asset, to_rpl))

        total = len(rpl_infos)
        written = 0
        seen: set[Tuple[str, int, float, float]] = set()

        for idx, info in enumerate(rpl_infos):
            if feedback.isCanceled():
                break

            feedback.setProgress(int(100 * (idx / max(1, total))))

            rpl_geom = info.geom
            if rpl_geom.isEmpty():
                continue

            # For each asset layer, query nearby candidates using a bbox in asset CRS
            for (asset_layer, asset_index, to_asset, to_rpl) in asset_prepared:
                try:
                    rpl_bbox_geom = QgsGeometry.fromRect(rpl_geom.boundingBox())
                    rpl_bbox_geom.transform(to_asset)
                    asset_bbox = rpl_bbox_geom.boundingBox()
                except Exception:
                    # If bbox transform fails, fall back to scanning all features
                    asset_bbox = None

                if asset_bbox is not None:
                    candidate_fids = asset_index.intersects(asset_bbox)
                    if not candidate_fids:
                        continue
                    req = QgsFeatureRequest().setFilterFids(candidate_fids)
                    candidates = asset_layer.getFeatures(req)
                else:
                    candidates = asset_layer.getFeatures()

                for asset_feat in candidates:
                    if feedback.isCanceled():
                        break

                    if not asset_feat.hasGeometry():
                        continue

                    asset_geom = QgsGeometry(asset_feat.geometry())
                    if asset_geom.isEmpty():
                        continue

                    # Transform asset geometry into RPL CRS
                    try:
                        asset_geom.transform(to_rpl)
                    except Exception:
                        continue

                    # Compute intersections
                    try:
                        inter = rpl_geom.intersection(asset_geom)
                    except Exception:
                        continue

                    points = _extract_points(inter)
                    if not points:
                        continue

                    asset_layer_name = asset_layer.name()

                    # Angles: use nearest segment in (transformed) CRS
                    for pt_xy in points:
                        key = (asset_layer.id(), int(asset_feat.id()), round(pt_xy.x(), 8), round(pt_xy.y(), 8))
                        if key in seen:
                            continue
                        seen.add(key)

                        kp_m = _kp_m_on_geom(pt_xy, rpl_geom, info.cumulative_base_m, distance_calculator)
                        kp_km = kp_m / 1000.0

                        # lat/lon via transform (store as lon/lat)
                        try:
                            wgs_pt = to_wgs84.transform(QgsPointXY(pt_xy))
                            lon = float(wgs_pt.x())
                            lat = float(wgs_pt.y())
                        except Exception:
                            lon = None
                            lat = None

                        rpl_dir = _direction_deg_of_nearest_segment(rpl_geom, pt_xy)
                        asset_dir = _direction_deg_of_nearest_segment(asset_geom, pt_xy)
                        cross_ang = _relative_angle_deg(rpl_dir, asset_dir)

                        # Fill unioned asset attributes
                        asset_values: List[Any] = []
                        for (out_name, src_name) in asset_attr_mapping:
                            try:
                                v = asset_feat[src_name] if src_name in asset_feat.fields().names() else None
                            except Exception:
                                v = None
                            # If a field type conflict was detected, it was created as string.
                            try:
                                if fields.field(out_name).type() == QVariant.String and v is not None:
                                    v = str(v)
                            except Exception:
                                pass
                            asset_values.append(v)

                        out_f = QgsFeature(fields)
                        out_f.setGeometry(QgsGeometry.fromPointXY(pt_xy))
                        out_f.setAttributes(
                            [
                                rpl_layer_name,
                                asset_layer_name,
                                int(info.fid),
                                int(asset_feat.id()),
                                round(kp_km, 4),
                                lat,
                                lon,
                                (round(cross_ang, 2) if cross_ang is not None else None),
                            ] + asset_values
                        )
                        sink.addFeature(out_f, QgsFeatureSink.FastInsert)
                        written += 1

                        # Optional buffer polygon around asset feature near crossing
                        if buffer_sink is not None and lon is not None and lat is not None and buffer_dist_m > 0.0:
                            try:
                                local_crs = _make_local_aeqd_crs(lat, lon)
                                to_local = QgsCoordinateTransform(rpl_crs, local_crs, context.transformContext())
                                from_local = QgsCoordinateTransform(local_crs, rpl_crs, context.transformContext())

                                asset_local = QgsGeometry(asset_geom)
                                asset_local.transform(to_local)
                                pt_local_xy = to_local.transform(QgsPointXY(pt_xy))

                                target_along = _nearest_along_m_planar(asset_local, QgsPointXY(pt_local_xy))
                                if target_along is None:
                                    continue

                                if buffer_half_len_m > 0.0:
                                    seg_start = max(0.0, target_along - buffer_half_len_m)
                                    seg_end = target_along + buffer_half_len_m
                                    clip_geom = _extract_subline_planar(asset_local, seg_start, seg_end)
                                else:
                                    clip_geom = asset_local

                                if clip_geom is None or clip_geom.isEmpty():
                                    continue

                                poly_local = clip_geom.buffer(buffer_dist_m, 24)
                                if poly_local is None or poly_local.isEmpty():
                                    continue

                                poly = QgsGeometry(poly_local)
                                poly.transform(from_local)

                                buf_f = QgsFeature(buffer_fields)
                                buf_f.setGeometry(poly)
                                buf_f.setAttributes(
                                    [
                                        rpl_layer_name,
                                        asset_layer_name,
                                        int(info.fid),
                                        int(asset_feat.id()),
                                        round(kp_km, 4),
                                        (round(cross_ang, 2) if cross_ang is not None else None),
                                        buffer_half_len_m,
                                        buffer_dist_m,
                                    ] + asset_values
                                )
                                buffer_sink.addFeature(buf_f, QgsFeatureSink.FastInsert)
                            except Exception:
                                # Don't fail the whole algorithm if buffering fails for one feature.
                                feedback.pushWarning('Failed to create buffer polygon for a crossing.')

        feedback.pushInfo(f"Identified {written} crossing point(s).")

        # Dynamic output naming
        self.renamer = Renamer(f"{rpl_layer_name}_CX_Listing")
        context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(self.renamer)

        result = {self.OUTPUT: dest_id}

        if buffer_dest_id is not None:
            self.buffer_renamer = Renamer(f"{rpl_layer_name}_CX_Buffer")
            context.layerToLoadOnCompletionDetails(buffer_dest_id).setPostProcessor(self.buffer_renamer)
            result[self.OUTPUT_BUFFER] = buffer_dest_id

        return result

    def name(self):
        return 'identify_rpl_crossing_points'

    def displayName(self):
        return self.tr('Identify RPL Crossing Points')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def shortHelpString(self):
        return self.tr(
            """
Creates a point layer at every intersection between an input RPL line layer and one-or-more asset line layers.

**Inputs**
- RPL Line Layer: The reference route (supports multi-feature RPL layers).
- Assets Line Layer(s): One or more line layers to test for crossings.

**Output fields**
- kp: KP along the RPL (km)
- lat/lon: crossing point coordinates in EPSG:4326
- cross_ang: relative crossing angle between the two line segments (degrees, 0-180)
- rpl_layer / asset_layer: input layer names
- asset_*: union of all fields from the selected asset layers (prefixed with 'asset_')

**Optional buffer output**
If you set a non-zero buffer distance (m), the tool also outputs a polygon layer where each polygon is a buffer around the crossed asset feature near the crossing:
- A line segment is clipped to X meters either side of the crossing point along the asset line (if X > 0)
- That segment is buffered by Y meters (if Y > 0)

Buffering is done in a local meters-based CRS (AEQD) per-crossing for accurate distances even when inputs are in a geographic CRS.

The output layer is automatically named with a '_CX_Listing' suffix based on the input RPL layer name.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return IdentifyRPLCrossingPointsAlgorithm()


class Renamer(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, layer_name):
        self.name = layer_name
        super().__init__()

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.name)
