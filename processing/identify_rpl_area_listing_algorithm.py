# identify_rpl_area_listing_algorithm.py
# -*- coding: utf-8 -*-
"""IdentifyRPLAreaListingAlgorithm

Creates a line layer of route sections where an input RPL line intersects one-or-more polygon layers.

Output includes:
- Start/end KP (km) along the RPL (supports multi-feature RPL layers)
- Start/end lat/lon (EPSG:4326) for each extracted segment endpoint
- References to the input layer names and feature ids
- Attributes of the polygon feature (as separate columns, unioned across selected polygon layers)

The output layer is automatically named with a '_Area_Listing' suffix based on the input RPL layer name.

"""

from __future__ import annotations

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


def _extract_lines(geom: QgsGeometry) -> List[QgsGeometry]:
    """Extract LineString/MultiLineString parts from an intersection geometry."""

    if geom is None or geom.isEmpty():
        return []

    gtype = QgsWkbTypes.geometryType(geom.wkbType())

    if gtype == QgsWkbTypes.LineGeometry:
        if QgsWkbTypes.isMultiType(geom.wkbType()):
            try:
                return [QgsGeometry.fromPolylineXY(part) for part in geom.asMultiPolyline() if len(part) >= 2]
            except Exception:
                # Some providers return MultiCurve etc; fall through.
                pass
        try:
            part = geom.asPolyline()
            if len(part) >= 2:
                return [QgsGeometry.fromPolylineXY(part)]
        except Exception:
            return []

    if QgsWkbTypes.isGeometryCollection(geom.wkbType()):
        out: List[QgsGeometry] = []
        try:
            for part in geom.asGeometryCollection():
                out.extend(_extract_lines(part))
        except Exception:
            return []
        return out

    return []


def _safe_field_base_name(name: str) -> str:
    name = (name or '').strip()
    if not name:
        return 'field'
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


def _polygon_field_definitions(polygon_layers) -> Tuple[List[Tuple[str, str]], List[QgsField]]:
    """Return a mapping and output fields for polygon attributes (union across layers).

    Returns:
        - mapping: list of tuples (output_field_name, source_field_name)
        - fields: list of QgsField for output

    Notes:
        - Output field names are prefixed with 'area_' to avoid collisions.
        - If the same source field name has conflicting types across layers, output is string.
    """

    seen_out_names: set[str] = set()
    for reserved in [
        'rpl_layer',
        'area_layer',
        'rpl_fid',
        'area_fid',
        'start_kp',
        'end_kp',
        'start_lat',
        'start_lon',
        'end_lat',
        'end_lon',
    ]:
        seen_out_names.add(reserved)

    chosen: Dict[str, QgsField] = {}
    conflicts: set[str] = set()

    for layer in polygon_layers:
        for fld in layer.fields():
            src_name = fld.name()
            if src_name in chosen and src_name not in conflicts:
                if chosen[src_name].type() != fld.type():
                    conflicts.add(src_name)
            else:
                chosen.setdefault(
                    src_name,
                    QgsField(src_name, fld.type(), fld.typeName(), fld.length(), fld.precision()),
                )

    mapping: List[Tuple[str, str]] = []
    out_fields: List[QgsField] = []
    for src_name in sorted(chosen.keys(), key=lambda s: (s or '').lower()):
        out_name = _unique_field_name(seen_out_names, f"area_{src_name}")
        if src_name in conflicts:
            # Use a generous fixed length to avoid provider failures (e.g., Shapefile).
            out_fields.append(QgsField(out_name, QVariant.String, '', 254, 0))
        else:
            f = chosen[src_name]
            # Many providers enforce hard string lengths. If the source field is short but values exceed it
            # (common for derived/edited fields), writing can fail. Prefer a safe length.
            if f.type() == QVariant.String:
                out_fields.append(QgsField(out_name, f.type(), f.typeName(), max(int(f.length() or 0), 254), int(f.precision() or 0)))
            else:
                out_fields.append(QgsField(out_name, f.type(), f.typeName(), f.length(), f.precision()))
        mapping.append((out_name, src_name))

    return mapping, out_fields


def _endpoints_xy(line_geom: QgsGeometry) -> Optional[Tuple[QgsPointXY, QgsPointXY]]:
    parts = _as_parts(line_geom)
    if not parts:
        return None

    # This helper is used only on already-extracted single-part lines.
    # Still handle multiple parts defensively by choosing the longest part.
    best = None  # (len_m, first, last)
    for part in parts:
        if len(part) < 2:
            continue
        first = QgsPointXY(part[0])
        last = QgsPointXY(part[-1])
        # length in layer units is fine for picking a representative part
        dx = float(last.x() - first.x())
        dy = float(last.y() - first.y())
        approx = dx * dx + dy * dy
        if best is None or approx > best[0]:
            best = (approx, first, last)

    if best is None:
        return None

    return best[1], best[2]


class IdentifyRPLAreaListingAlgorithm(QgsProcessingAlgorithm):
    INPUT_RPL = 'INPUT_RPL'
    INPUT_AREAS = 'INPUT_AREAS'
    OUTPUT = 'OUTPUT'

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
                self.INPUT_AREAS,
                self.tr('Area Polygon Layer(s)'),
                layerType=QgsProcessing.TypeVectorPolygon,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output layer'),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        rpl_source = self.parameterAsSource(parameters, self.INPUT_RPL, context)
        if rpl_source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_RPL))

        rpl_layer = self.parameterAsVectorLayer(parameters, self.INPUT_RPL, context)
        rpl_layer_name = rpl_layer.name() if rpl_layer is not None else (rpl_source.sourceName() or 'RPL')

        polygon_layers = self.parameterAsLayerList(parameters, self.INPUT_AREAS, context) or []
        polygon_layers = [lyr for lyr in polygon_layers if getattr(lyr, 'isValid', lambda: False)()]
        if not polygon_layers:
            raise QgsProcessingException(self.tr('No polygon layers were provided.'))

        rpl_crs = rpl_source.sourceCrs()

        distance_calculator = QgsDistanceArea()
        distance_calculator.setSourceCrs(rpl_crs, context.transformContext())
        distance_calculator.setEllipsoid(context.project().ellipsoid())

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

        area_attr_mapping, area_attr_fields = _polygon_field_definitions(polygon_layers)

        fields = QgsFields()
        fields.append(QgsField('rpl_layer', QVariant.String, '', 254, 0))
        fields.append(QgsField('area_layer', QVariant.String, '', 254, 0))
        fields.append(QgsField('rpl_fid', QVariant.LongLong))
        fields.append(QgsField('area_fid', QVariant.LongLong))
        # Keep KPs consistently at 3 decimal places.
        fields.append(QgsField('start_kp', QVariant.Double, '', 20, 3))
        fields.append(QgsField('end_kp', QVariant.Double, '', 20, 3))
        fields.append(QgsField('start_lat', QVariant.Double))
        fields.append(QgsField('start_lon', QVariant.Double))
        fields.append(QgsField('end_lat', QVariant.Double))
        fields.append(QgsField('end_lon', QVariant.Double))
        for f in area_attr_fields:
            fields.append(f)

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.LineString,
            rpl_crs,
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        to_wgs84 = QgsCoordinateTransform(rpl_crs, wgs84, context.transformContext())

        # Prepare per-area layer spatial index and transforms
        area_prepared = []
        for area_layer in polygon_layers:
            area_crs = area_layer.crs()
            to_area = QgsCoordinateTransform(rpl_crs, area_crs, context.transformContext())
            to_rpl = QgsCoordinateTransform(area_crs, rpl_crs, context.transformContext())
            index = QgsSpatialIndex(area_layer.getFeatures())
            area_prepared.append((area_layer, index, to_area, to_rpl))

        total = len(rpl_infos)
        written = 0

        for idx, info in enumerate(rpl_infos):
            if feedback.isCanceled():
                break

            feedback.setProgress(int(100 * (idx / max(1, total))))

            rpl_geom = info.geom
            if rpl_geom.isEmpty():
                continue

            for (area_layer, area_index, to_area, to_rpl) in area_prepared:
                try:
                    rpl_bbox_geom = QgsGeometry.fromRect(rpl_geom.boundingBox())
                    rpl_bbox_geom.transform(to_area)
                    area_bbox = rpl_bbox_geom.boundingBox()
                except Exception:
                    area_bbox = None

                if area_bbox is not None:
                    candidate_fids = area_index.intersects(area_bbox)
                    if not candidate_fids:
                        continue
                    req = QgsFeatureRequest().setFilterFids(candidate_fids)
                    candidates = area_layer.getFeatures(req)
                else:
                    candidates = area_layer.getFeatures()

                area_layer_name = area_layer.name()

                for area_feat in candidates:
                    if feedback.isCanceled():
                        break

                    if not area_feat.hasGeometry():
                        continue

                    area_geom = QgsGeometry(area_feat.geometry())
                    if area_geom.isEmpty():
                        continue

                    try:
                        area_geom.transform(to_rpl)
                    except Exception:
                        continue

                    try:
                        inter = rpl_geom.intersection(area_geom)
                    except Exception:
                        continue

                    line_parts = _extract_lines(inter)
                    if not line_parts:
                        continue

                    # Fill unioned area attributes
                    area_values: List[Any] = []
                    for (out_name, src_name) in area_attr_mapping:
                        try:
                            v = area_feat[src_name] if src_name in area_feat.fields().names() else None
                        except Exception:
                            v = None
                        try:
                            if fields.field(out_name).type() == QVariant.String and v is not None:
                                v = str(v)
                                max_len = int(fields.field(out_name).length() or 0)
                                if max_len > 0 and len(v) > max_len:
                                    v = v[:max_len]
                        except Exception:
                            pass
                        area_values.append(v)

                    for seg in line_parts:
                        if seg is None or seg.isEmpty():
                            continue

                        endpoints = _endpoints_xy(seg)
                        if endpoints is None:
                            continue

                        a_xy, b_xy = endpoints
                        kp_a_m = _kp_m_on_geom(a_xy, rpl_geom, info.cumulative_base_m, distance_calculator)
                        kp_b_m = _kp_m_on_geom(b_xy, rpl_geom, info.cumulative_base_m, distance_calculator)

                        # Normalize so start = smaller KP
                        if kp_a_m <= kp_b_m:
                            start_xy, end_xy = a_xy, b_xy
                            start_kp_km, end_kp_km = kp_a_m / 1000.0, kp_b_m / 1000.0
                        else:
                            start_xy, end_xy = b_xy, a_xy
                            start_kp_km, end_kp_km = kp_b_m / 1000.0, kp_a_m / 1000.0

                        # Start/end lat/lon
                        try:
                            start_wgs = to_wgs84.transform(QgsPointXY(start_xy))
                            start_lon = float(start_wgs.x())
                            start_lat = float(start_wgs.y())
                        except Exception:
                            start_lon = None
                            start_lat = None

                        try:
                            end_wgs = to_wgs84.transform(QgsPointXY(end_xy))
                            end_lon = float(end_wgs.x())
                            end_lat = float(end_wgs.y())
                        except Exception:
                            end_lon = None
                            end_lat = None

                        out_f = QgsFeature(fields)
                        out_f.setGeometry(seg)
                        out_f.setAttributes(
                            [
                                rpl_layer_name,
                                area_layer_name,
                                int(info.fid),
                                int(area_feat.id()),
                                round(start_kp_km, 3),
                                round(end_kp_km, 3),
                                start_lat,
                                start_lon,
                                end_lat,
                                end_lon,
                            ]
                            + area_values
                        )
                        sink.addFeature(out_f, QgsFeatureSink.FastInsert)
                        written += 1

        feedback.pushInfo(f"Created {written} segment(s) from polygon intersections.")

        # Dynamic output naming
        self.renamer = Renamer(f"{rpl_layer_name}_Area_Listing")
        context.layerToLoadOnCompletionDetails(dest_id).setPostProcessor(self.renamer)

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'identify_rpl_area_listing'

    def displayName(self):
        return self.tr('Identify RPL Area Listing')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def shortHelpString(self):
        return self.tr(
            """
Creates a line layer which highlights the sections of an input RPL route which pass through one-or-more polygon layers.

**Inputs**
- RPL Line Layer: The reference route (supports multi-feature RPL layers).
- Area Polygon Layer(s): One or more polygon layers to test for intersections.

**Output fields**
- start_kp/end_kp: KP range (km) for each extracted route segment
- start_lat/start_lon and end_lat/end_lon: segment endpoint coordinates in EPSG:4326
- rpl_layer/area_layer: input layer names
- rpl_fid/area_fid: input feature ids
- area_*: union of all fields from the selected polygon layers (prefixed with 'area_')

The output layer is automatically named with a '_Area_Listing' suffix based on the input RPL layer name.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return IdentifyRPLAreaListingAlgorithm()


class Renamer(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, layer_name):
        self.name = layer_name
        super().__init__()

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.name)
