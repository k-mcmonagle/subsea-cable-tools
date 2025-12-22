# identify_rpl_lay_corridor_proximity_listing_algorithm.py
# -*- coding: utf-8 -*-
"""IdentifyRPLLayCorridorProximityListingAlgorithm

Creates listing layers containing all input features which intersect/encroach
an input Lay Corridor (polygon) layer.

Produces up to three listings (depending on which inputs are provided):
- Point proximity listing
- Line proximity listing
- Area (polygon) proximity listing

Each output includes:
- Source layer name + feature id
- JSON-encoded copy of all source attributes
- Representative lat/lon in EPSG:4326
- KP (km) and DCC (m) to the input RPL line (via existing RPLComparator logic)
- Reference to the input RPL layer name

Output layers are automatically named using the Lay Corridor layer name with
suffixes:
- _Point_Prox_Listing
- _Line_Prox_Listing
- _Area_Prox_Listing
"""

from __future__ import annotations

import json

from typing import Any, Dict, List, Optional, Set

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsFeatureSink,
    QgsFields,
    QgsField,
    QgsGeometry,
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
    QgsVectorLayer,
    QgsWkbTypes,
)

from .rpl_comparison_utils import RPLComparator


def _safe_field_base_name(name: str) -> str:
    name = (name or '').strip()
    if not name:
        return 'field'
    return name[:60]


def _unique_field_name(existing: Set[str], base: str) -> str:
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


def _json_safe(value: Any) -> Any:
    """Best-effort conversion of QGIS/PyQt values into JSON-serialisable types."""
    if value is None:
        return None
    try:
        # QGIS often gives Python primitives already.
        if isinstance(value, (str, int, float, bool)):
            return value
        # QVariant and other types: coerce to string as a safe fallback.
        return str(value)
    except Exception:
        return None


class IdentifyRPLLayCorridorProximityListingAlgorithm(QgsProcessingAlgorithm):
    INPUT_RPL = 'INPUT_RPL'
    INPUT_LAY_CORRIDOR = 'INPUT_LAY_CORRIDOR'
    INPUT_POINTS = 'INPUT_POINTS'
    INPUT_LINES = 'INPUT_LINES'
    INPUT_AREAS = 'INPUT_AREAS'

    OUTPUT = 'OUTPUT'  # points (kept for backwards compatibility)
    OUTPUT_LINES = 'OUTPUT_LINES'
    OUTPUT_AREAS = 'OUTPUT_AREAS'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_RPL,
                self.tr('Input RPL line layer'),
                [QgsProcessing.TypeVectorLine],
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_LAY_CORRIDOR,
                self.tr('Input Lay Corridor layer (polygon)'),
                [QgsProcessing.TypeVectorPolygon],
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_POINTS,
                self.tr('Input point layer(s)'),
                layerType=QgsProcessing.TypeVectorPoint,
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LINES,
                self.tr('Input line layer(s)'),
                layerType=QgsProcessing.TypeVectorLine,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_AREAS,
                self.tr('Input polygon layer(s)'),
                layerType=QgsProcessing.TypeVectorPolygon,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output point proximity listing'),
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_LINES,
                self.tr('Output line proximity listing'),
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_AREAS,
                self.tr('Output area proximity listing'),
                optional=True,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        rpl_layer = self.parameterAsVectorLayer(parameters, self.INPUT_RPL, context)
        corridor_layer = self.parameterAsVectorLayer(parameters, self.INPUT_LAY_CORRIDOR, context)

        point_layers = self.parameterAsLayerList(parameters, self.INPUT_POINTS, context) or []
        point_layers = [lyr for lyr in point_layers if isinstance(lyr, QgsVectorLayer)]

        line_layers = self.parameterAsLayerList(parameters, self.INPUT_LINES, context) or []
        line_layers = [lyr for lyr in line_layers if isinstance(lyr, QgsVectorLayer)]

        area_layers = self.parameterAsLayerList(parameters, self.INPUT_AREAS, context) or []
        area_layers = [lyr for lyr in area_layers if isinstance(lyr, QgsVectorLayer)]

        if rpl_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_RPL))
        if corridor_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_LAY_CORRIDOR))
        if not point_layers and not line_layers and not area_layers:
            raise QgsProcessingException(self.tr('Please provide at least one point, line, or polygon layer.'))

        # Basic geometry type validation
        if QgsWkbTypes.geometryType(rpl_layer.wkbType()) != QgsWkbTypes.LineGeometry:
            raise QgsProcessingException(self.tr('Input RPL layer must be a line layer.'))
        if QgsWkbTypes.geometryType(corridor_layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise QgsProcessingException(self.tr('Input Lay Corridor layer must be a polygon layer.'))
        for lyr in point_layers:
            if QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.PointGeometry:
                raise QgsProcessingException(self.tr(f"Layer '{lyr.name()}' is not a point layer."))

        for lyr in line_layers:
            if QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.LineGeometry:
                raise QgsProcessingException(self.tr(f"Layer '{lyr.name()}' is not a line layer."))

        for lyr in area_layers:
            if QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.PolygonGeometry:
                raise QgsProcessingException(self.tr(f"Layer '{lyr.name()}' is not a polygon layer."))

        # CRS handling:
        # - Use the Lay Corridor CRS for corridor intersection tests and output geometry.
        # - Use the RPL CRS for KP/DCC calculations.
        out_crs = corridor_layer.sourceCrs()
        rpl_crs = rpl_layer.sourceCrs()

        if not out_crs.isValid():
            raise QgsProcessingException(self.tr('Lay Corridor layer has an invalid/unknown CRS.'))
        if not rpl_crs.isValid():
            raise QgsProcessingException(self.tr('RPL layer has an invalid/unknown CRS.'))

        def _layer_crs_or_fallback(layer: QgsVectorLayer, label: str) -> QgsCoordinateReferenceSystem:
            layer_crs = layer.sourceCrs()
            if layer_crs is None or not layer_crs.isValid():
                feedback.pushWarning(
                    self.tr(
                        "Layer '{name}' has no valid CRS; assuming it matches the Lay Corridor CRS ({crs})."
                    ).format(name=layer.name(), crs=out_crs.authid())
                )
                return out_crs
            return layer_crs

        # Output schema: keep a robust per-feature JSON payload rather than trying to union fields.
        # This avoids NULLs caused by provider field-name truncation (e.g. Shapefile) and differing
        # schemas between input point layers.
        out_fields = QgsFields()
        existing_names: Set[str] = set()

        src_layer_name_field = _unique_field_name(existing_names, 'source_layer')
        src_fid_field = _unique_field_name(existing_names, 'source_fid')
        src_attrs_field = _unique_field_name(existing_names, 'source_attrs')
        lat_field = _unique_field_name(existing_names, 'lat')
        lon_field = _unique_field_name(existing_names, 'lon')
        rpl_kp_field = _unique_field_name(existing_names, 'rpl_kp')
        rpl_dcc_field = _unique_field_name(existing_names, 'rpl_dcc')
        rpl_ref_field = _unique_field_name(existing_names, 'rpl_ref')

        out_fields.append(QgsField(src_layer_name_field, QVariant.String, '', 254, 0))
        out_fields.append(QgsField(src_fid_field, QVariant.Int))
        # Prefer a generous length (works well in GPKG/Memory). Shapefile will truncate to 254.
        out_fields.append(QgsField(src_attrs_field, QVariant.String, '', 10000, 0))
        out_fields.append(QgsField(lat_field, QVariant.Double))
        out_fields.append(QgsField(lon_field, QVariant.Double))
        out_fields.append(QgsField(rpl_kp_field, QVariant.Double))
        out_fields.append(QgsField(rpl_dcc_field, QVariant.Double))
        out_fields.append(QgsField(rpl_ref_field, QVariant.String, '', 254, 0))

        (point_sink, point_dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            out_fields,
            QgsWkbTypes.Point,
            out_crs,
        )

        if point_sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        (line_sink, line_dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT_LINES,
            context,
            out_fields,
            QgsWkbTypes.MultiLineString,
            out_crs,
        )

        (area_sink, area_dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT_AREAS,
            context,
            out_fields,
            QgsWkbTypes.MultiPolygon,
            out_crs,
        )

        # Coordinate transform for lat/lon output (from output CRS)
        wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
        try:
            to_wgs84 = QgsCoordinateTransform(out_crs, wgs84, context.transformContext())
        except Exception:
            # Fallback: try project-based transform context
            to_wgs84 = QgsCoordinateTransform(out_crs, wgs84, QgsProject.instance())

        # Spatial index for corridor polygons
        corridor_index = QgsSpatialIndex()
        corridor_geoms: Dict[int, QgsGeometry] = {}
        for feat in corridor_layer.getFeatures(QgsFeatureRequest().setSubsetOfAttributes([])):
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            corridor_index.addFeature(feat)
            corridor_geoms[int(feat.id())] = QgsGeometry(geom)

        if not corridor_geoms:
            feedback.pushInfo(self.tr('Lay Corridor has no polygon geometry; output will be empty.'))

        # KP/DCC calculator on RPL (in RPL CRS)
        try:
            comparator = RPLComparator(rpl_layer, rpl_layer, rpl_crs, context)
        except Exception as e:
            raise QgsProcessingException(self.tr(f'Failed to initialize RPL comparator: {str(e)}'))

        # Build a single geometry for RPL to support robust nearest-point logic for lines/polygons
        rpl_geom_parts: List[QgsGeometry] = []
        for feat in rpl_layer.getFeatures(QgsFeatureRequest().setSubsetOfAttributes([])):
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            rpl_geom_parts.append(QgsGeometry(geom))

        if not rpl_geom_parts:
            raise QgsProcessingException(self.tr('Input RPL layer has no line geometry.'))

        try:
            rpl_union = QgsGeometry.unaryUnion(rpl_geom_parts) if len(rpl_geom_parts) > 1 else rpl_geom_parts[0]
        except Exception:
            # Fallback: union can fail on some invalid geometries; just use first feature geometry.
            rpl_union = rpl_geom_parts[0]

        rpl_name = rpl_layer.name()

        # Output layer attribute positions
        out_name_to_idx = {out_fields.at(i).name(): i for i in range(out_fields.count())}
        idx_src_layer = out_name_to_idx[src_layer_name_field]
        idx_src_fid = out_name_to_idx[src_fid_field]
        idx_src_attrs = out_name_to_idx[src_attrs_field]
        idx_lat = out_name_to_idx[lat_field]
        idx_lon = out_name_to_idx[lon_field]
        idx_rpl_kp = out_name_to_idx[rpl_kp_field]
        idx_rpl_dcc = out_name_to_idx[rpl_dcc_field]
        idx_rpl_ref = out_name_to_idx[rpl_ref_field]

        def _representative_point_xy(feature_geom: QgsGeometry) -> Optional[QgsPointXY]:
            if feature_geom is None or feature_geom.isEmpty():
                return None
            try:
                if QgsWkbTypes.geometryType(feature_geom.wkbType()) == QgsWkbTypes.PointGeometry:
                    return QgsPointXY(feature_geom.asPoint())
            except Exception:
                pass

            # Prefer a point that is stable and guaranteed inside polygons.
            try:
                pos = feature_geom.pointOnSurface()
                if pos is not None and not pos.isEmpty():
                    return QgsPointXY(pos.asPoint())
            except Exception:
                pass

            try:
                c = feature_geom.centroid()
                if c is not None and not c.isEmpty():
                    return QgsPointXY(c.asPoint())
            except Exception:
                pass

            try:
                bb = feature_geom.boundingBox()
                return QgsPointXY(bb.center())
            except Exception:
                return None

        def _nearest_point_for_kp_dcc(feature_geom: QgsGeometry) -> Optional[QgsPointXY]:
            if feature_geom is None or feature_geom.isEmpty() or rpl_union is None or rpl_union.isEmpty():
                return None
            try:
                sl = feature_geom.shortestLine(rpl_union)
                if sl is None or sl.isEmpty():
                    return None

                if sl.isMultipart():
                    parts = sl.asMultiPolyline()
                    if parts and parts[0]:
                        return QgsPointXY(parts[0][0])
                else:
                    pts = sl.asPolyline()
                    if pts:
                        return QgsPointXY(pts[0])
            except Exception:
                return None
            return None

        def _corridor_intersects(feature_geom: QgsGeometry) -> bool:
            if feature_geom is None or feature_geom.isEmpty():
                return False
            for cand_fid in corridor_index.intersects(feature_geom.boundingBox()):
                poly = corridor_geoms.get(int(cand_fid))
                if poly is None or poly.isEmpty():
                    continue
                if poly.intersects(feature_geom):
                    return True
            return False

        def _attrs_json_for_feature(src_layer: QgsVectorLayer, feature: QgsFeature) -> Optional[str]:
            try:
                field_names = [src_layer.fields().at(i).name() for i in range(src_layer.fields().count())]
                values = feature.attributes()
                payload = {field_names[i]: _json_safe(values[i]) for i in range(min(len(field_names), len(values)))}
                return json.dumps(payload, ensure_ascii=False)
            except Exception:
                return None

        def _write_listing(
            layers: List[QgsVectorLayer],
            sink: Optional[QgsFeatureSink],
            label: str,
        ) -> Dict[str, int]:
            if sink is None:
                return {'written': 0, 'skipped': 0}
            if not layers:
                feedback.pushInfo(self.tr(f'No {label} layers provided; output will be empty.'))
                return {'written': 0, 'skipped': 0}

            written = 0
            skipped = 0
            for lyr in layers:
                if feedback.isCanceled():
                    break

                layer_crs = _layer_crs_or_fallback(lyr, label)

                try:
                    to_out = (
                        None
                        if layer_crs == out_crs
                        else QgsCoordinateTransform(layer_crs, out_crs, context.transformContext())
                    )
                except Exception:
                    to_out = QgsCoordinateTransform(layer_crs, out_crs, QgsProject.instance())

                try:
                    to_rpl = (
                        None
                        if layer_crs == rpl_crs
                        else QgsCoordinateTransform(layer_crs, rpl_crs, context.transformContext())
                    )
                except Exception:
                    to_rpl = QgsCoordinateTransform(layer_crs, rpl_crs, QgsProject.instance())

                for feat in lyr.getFeatures():
                    if feedback.isCanceled():
                        break

                    src_geom = feat.geometry()
                    if src_geom is None or src_geom.isEmpty():
                        skipped += 1
                        continue

                    # Transform geometry to output CRS for corridor test + output
                    geom_out = QgsGeometry(src_geom)
                    if to_out is not None:
                        try:
                            geom_out.transform(to_out)
                        except Exception:
                            skipped += 1
                            continue

                    if not _corridor_intersects(geom_out):
                        continue

                    # Transform geometry to RPL CRS for KP/DCC
                    geom_rpl = QgsGeometry(src_geom)
                    if to_rpl is not None:
                        try:
                            geom_rpl.transform(to_rpl)
                        except Exception:
                            skipped += 1
                            continue

                    # Use nearest point on feature to RPL for KP/DCC; fallback to representative point
                    kp_pt_rpl = _nearest_point_for_kp_dcc(geom_rpl) or _representative_point_xy(geom_rpl)
                    if kp_pt_rpl is None:
                        skipped += 1
                        continue

                    rep_pt_out = _representative_point_xy(geom_out)

                    try:
                        kp_km = comparator.calculate_kp_to_point(kp_pt_rpl, source=True)
                        dcc_m = comparator.distance_cross_course(kp_pt_rpl, source=True)
                    except Exception as e:
                        feedback.pushWarning(
                            self.tr(f"Failed KP/DCC for feature {feat.id()} in '{lyr.name()}': {str(e)}")
                        )
                        skipped += 1
                        continue

                    try:
                        if rep_pt_out is not None:
                            wgs_pt = to_wgs84.transform(QgsPointXY(rep_pt_out))
                            lon = float(wgs_pt.x())
                            lat = float(wgs_pt.y())
                        else:
                            lon = None
                            lat = None
                    except Exception:
                        lon = None
                        lat = None

                    attrs_json = _attrs_json_for_feature(lyr, feat)

                    out_feat = QgsFeature(out_fields)
                    out_feat.setGeometry(geom_out)

                    attrs = [None] * out_fields.count()
                    attrs[idx_src_layer] = lyr.name()
                    attrs[idx_src_fid] = int(feat.id())
                    attrs[idx_src_attrs] = attrs_json
                    attrs[idx_lat] = lat
                    attrs[idx_lon] = lon
                    attrs[idx_rpl_kp] = round(float(kp_km), 3) if kp_km is not None else None
                    attrs[idx_rpl_dcc] = round(float(dcc_m), 3) if dcc_m is not None else None
                    attrs[idx_rpl_ref] = rpl_name
                    out_feat.setAttributes(attrs)

                    sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                    written += 1
            return {'written': written, 'skipped': skipped}

        # Progress across all input layers
        total = (
            sum(int(lyr.featureCount() or 0) for lyr in point_layers)
            + sum(int(lyr.featureCount() or 0) for lyr in line_layers)
            + sum(int(lyr.featureCount() or 0) for lyr in area_layers)
        )
        done = 0

        def _tick_progress(n: int = 1):
            nonlocal done
            done += n
            if total > 0:
                feedback.setProgress(int(done / total * 100))

        # Wrap sinks to ensure progress updates still occur.
        # (We iterate features directly in _write_listing; simplest approach is to update progress here via featureCount())
        # Use coarse progress per-layer rather than per-feature for performance.
        if total > 0:
            # Seed progress with 0; subsequent per-layer steps will update.
            feedback.setProgress(0)

        point_stats = _write_listing(point_layers, point_sink, 'point')
        _tick_progress(sum(int(lyr.featureCount() or 0) for lyr in point_layers))

        line_stats = _write_listing(line_layers, line_sink, 'line')
        _tick_progress(sum(int(lyr.featureCount() or 0) for lyr in line_layers))

        area_stats = _write_listing(area_layers, area_sink, 'polygon')
        _tick_progress(sum(int(lyr.featureCount() or 0) for lyr in area_layers))

        feedback.pushInfo(self.tr(f"Wrote {point_stats['written']} point(s) intersecting the lay corridor."))
        feedback.pushInfo(self.tr(f"Wrote {line_stats['written']} line(s) intersecting the lay corridor."))
        feedback.pushInfo(self.tr(f"Wrote {area_stats['written']} polygon(s) intersecting the lay corridor."))

        skipped = point_stats['skipped'] + line_stats['skipped'] + area_stats['skipped']
        if skipped:
            feedback.pushInfo(self.tr(f'Skipped {skipped} feature(s) due to invalid geometry or errors.'))

        # Dynamic output naming based on the corridor layer name
        corridor_name = corridor_layer.name()
        self.renamer_points = Renamer(f"{corridor_name}_Point_Prox_Listing")
        context.layerToLoadOnCompletionDetails(point_dest_id).setPostProcessor(self.renamer_points)

        if line_dest_id:
            self.renamer_lines = Renamer(f"{corridor_name}_Line_Prox_Listing")
            context.layerToLoadOnCompletionDetails(line_dest_id).setPostProcessor(self.renamer_lines)

        if area_dest_id:
            self.renamer_areas = Renamer(f"{corridor_name}_Area_Prox_Listing")
            context.layerToLoadOnCompletionDetails(area_dest_id).setPostProcessor(self.renamer_areas)

        return {
            self.OUTPUT: point_dest_id,
            self.OUTPUT_LINES: line_dest_id,
            self.OUTPUT_AREAS: area_dest_id,
        }

    def name(self):
        return 'identify_rpl_lay_corridor_proximity_listing'

    def displayName(self):
        return self.tr('Identify RPL Lay Corridor Proximity Listing')

    def group(self):
        return self.tr('RPL Tools')

    def groupId(self):
        return 'rpl_tools'

    def shortHelpString(self):
        return self.tr(
            """
Creates listing layers containing all provided point/line/polygon features which intersect (encroach) an input Lay Corridor polygon layer.

**Inputs**
- RPL Line Layer: reference route used to compute KP and DCC.
- Lay Corridor Layer: polygon bounds (typically created by the Dynamic Buffer tool).
- Point Layer(s): one or more point layers to test.
- Line Layer(s): optional line layer(s) to test.
- Polygon Layer(s): optional polygon layer(s) to test.

**Output fields**
- source_layer: name of the source layer the feature came from
- source_fid: original feature id in that source layer
- source_attrs: JSON-encoded dictionary of all original attributes from the source feature
- lat/lon: representative coordinates in EPSG:4326
- rpl_kp: KP (km) on the RPL nearest to the feature
- rpl_dcc: DCC (m) distance-cross-course to the RPL (computed using the nearest point on the feature geometry)
- rpl_ref: RPL layer name

**CRS handling**
- Inputs may use different CRSs. Features are reprojected on-the-fly for corridor intersection tests (Lay Corridor CRS) and for KP/DCC calculations (RPL CRS).
- If an input layer has no valid CRS assigned, the algorithm will assume it matches the Lay Corridor CRS and will emit a warning.

Outputs are automatically named using the Lay Corridor layer name with suffixes:
- _Point_Prox_Listing
- _Line_Prox_Listing
- _Area_Prox_Listing
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return IdentifyRPLLayCorridorProximityListingAlgorithm()


class Renamer(QgsProcessingLayerPostProcessorInterface):
    def __init__(self, layer_name):
        self.name = layer_name
        super().__init__()

    def postProcessLayer(self, layer, context, feedback):
        layer.setName(self.name)
