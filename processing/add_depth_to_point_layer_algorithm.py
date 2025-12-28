# -*- coding: utf-8 -*-
"""AddDepthToPointLayerAlgorithm

Adds depth/elevation attributes to an input point layer by sampling:
- one or more raster layers (via provider.sample), and/or
- one or more contour line layers (nearest feature, with optional search radius).

Designed for KP point workflows.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsDistanceArea,
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
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)


class AddDepthToPointLayerAlgorithm(QgsProcessingAlgorithm):
    INPUT = 'INPUT'
    DEPTH_SOURCE = 'DEPTH_SOURCE'

    RASTER_LAYERS = 'RASTER_LAYERS'
    RASTER_BAND = 'RASTER_BAND'

    CONTOUR_LAYER_1 = 'CONTOUR_LAYER_1'
    CONTOUR_DEPTH_FIELD_1 = 'CONTOUR_DEPTH_FIELD_1'
    CONTOUR_LAYER_2 = 'CONTOUR_LAYER_2'
    CONTOUR_DEPTH_FIELD_2 = 'CONTOUR_DEPTH_FIELD_2'
    CONTOUR_SEARCH_RADIUS_M = 'CONTOUR_SEARCH_RADIUS_M'

    USE_ABS_DEPTH = 'USE_ABS_DEPTH'
    OUTPUT_MODE = 'OUTPUT_MODE'

    OUTPUT = 'OUTPUT'

    @staticmethod
    def _safe_field_name(name: str, used: set) -> str:
        name = (name or '').strip()
        if not name:
            name = 'depth'
        name = name.lower()
        name = re.sub(r"\s+", "_", name)
        name = re.sub(r"[^a-z0-9_]+", "", name)
        if not name:
            name = 'depth'

        base = name
        i = 2
        while name in used:
            name = f"{base}_{i}"
            i += 1
        used.add(name)
        return name

    @staticmethod
    def _build_raster_samplers(
        rasters: Sequence[QgsRasterLayer],
        points_crs,
    ) -> List[Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]]:
        samplers: List[Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]] = []
        for r in rasters:
            if not r:
                continue
            transform = None
            if r.crs() != points_crs:
                try:
                    transform = QgsCoordinateTransform(points_crs, r.crs(), QgsProject.instance())
                except Exception:
                    transform = None
            samplers.append((r, transform))
        return samplers

    @staticmethod
    def _build_contour_samplers(
        contour_layers: Sequence[Optional[QgsVectorLayer]],
        depth_fields: Sequence[str],
        points_crs,
    ) -> List[Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]]:
        out: List[Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]] = []
        for i, lyr in enumerate(contour_layers):
            if not lyr:
                continue
            depth_field = depth_fields[i] if i < len(depth_fields) else ''
            transform = None
            if lyr.crs() != points_crs:
                try:
                    transform = QgsCoordinateTransform(points_crs, lyr.crs(), QgsProject.instance())
                except Exception:
                    transform = None
            out.append((lyr, depth_field, transform))
        return out

    @staticmethod
    def _sample_rasters(
        point: QgsPointXY,
        raster_samplers: Sequence[Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]],
        band: int,
    ) -> Tuple[Optional[float], Optional[str], List[Tuple[str, Optional[float]]]]:
        """Return (best_value, best_source_name, all_values)."""

        best_val: Optional[float] = None
        best_src: Optional[str] = None
        all_vals: List[Tuple[str, Optional[float]]] = []

        band = int(band) if band and int(band) > 0 else 1

        for raster, transform in raster_samplers:
            sample_pt = point
            if transform is not None:
                try:
                    sample_pt = transform.transform(point)
                except Exception:
                    all_vals.append((raster.name(), None))
                    continue

            try:
                val, ok = raster.dataProvider().sample(sample_pt, band)
            except Exception:
                ok = False
                val = None

            if ok and val is not None:
                try:
                    fval = float(val)
                except Exception:
                    fval = None
            else:
                fval = None

            all_vals.append((raster.name(), fval))
            if best_val is None and fval is not None:
                best_val = fval
                best_src = raster.name()

        return best_val, best_src, all_vals

    @staticmethod
    def _sample_contours(
        point: QgsPointXY,
        contour_samplers: Sequence[Tuple[QgsVectorLayer, str, Optional[QgsCoordinateTransform]]],
        search_radius_m: float,
        context,
    ) -> Tuple[Optional[float], Optional[str], Optional[float]]:
        """Return (best_depth, best_source_layer_name, best_distance_m)."""

        best_depth = None
        best_dist = None
        best_src = None

        for lyr, depth_field, transform in contour_samplers:
            if not lyr:
                continue

            query_point = point
            if transform is not None:
                try:
                    query_point = transform.transform(point)
                except Exception:
                    continue

            pt_geom = QgsGeometry.fromPointXY(query_point)

            # Filter candidates by bbox if a search radius is provided.
            feat_iter = None
            if search_radius_m and search_radius_m > 0:
                if lyr.crs().isGeographic():
                    # Approx meters -> degrees
                    import math

                    lat = query_point.y()
                    deg_lat = search_radius_m / 111320.0
                    cos_lat = max(0.1, abs(math.cos(math.radians(lat))))
                    deg_lon = search_radius_m / (111320.0 * cos_lat)
                    rect = pt_geom.boundingBox()
                    rect.setXMinimum(rect.xMinimum() - deg_lon)
                    rect.setXMaximum(rect.xMaximum() + deg_lon)
                    rect.setYMinimum(rect.yMinimum() - deg_lat)
                    rect.setYMaximum(rect.yMaximum() + deg_lat)
                    request = QgsFeatureRequest().setFilterRect(rect)
                    feat_iter = lyr.getFeatures(request)
                else:
                    rect = pt_geom.buffer(search_radius_m, 8).boundingBox()
                    request = QgsFeatureRequest().setFilterRect(rect)
                    feat_iter = lyr.getFeatures(request)

            if feat_iter is None:
                feat_iter = lyr.getFeatures()

            dist_area = None
            if lyr.crs().isGeographic():
                dist_area = QgsDistanceArea()
                dist_area.setSourceCrs(lyr.crs(), context.transformContext())
                dist_area.setEllipsoid(context.project().ellipsoid())

            for feat in feat_iter:
                g = feat.geometry()
                if not g or g.isEmpty():
                    continue

                if dist_area is None:
                    dist = float(g.distance(pt_geom))
                else:
                    try:
                        closest = g.closestPoint(pt_geom)
                        closest_pt = closest.asPoint() if not closest.isEmpty() else None
                        if closest_pt is None:
                            continue
                        dist = float(dist_area.measureLine(query_point, QgsPointXY(closest_pt)))
                    except Exception:
                        continue

                if search_radius_m and search_radius_m > 0 and dist > search_radius_m:
                    continue

                if best_dist is None or dist < best_dist:
                    # Extract depth/elevation
                    if depth_field and depth_field in feat.fields().names():
                        z = feat[depth_field]
                    else:
                        names = feat.fields().names()
                        z = feat[names[0]] if names else None
                    if z is None:
                        continue
                    try:
                        zf = float(z)
                    except Exception:
                        continue

                    best_dist = dist
                    best_depth = zf
                    best_src = lyr.name()

        return best_depth, best_src, best_dist

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                self.tr('Input point layer'),
                [QgsProcessing.TypeVectorPoint],
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.DEPTH_SOURCE,
                self.tr('Depth source'),
                options=[self.tr('Auto (prefer raster, fallback to contours)'), self.tr('Raster only'), self.tr('Contours only')],
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.RASTER_LAYERS,
                self.tr('Raster layers (depth)'),
                layerType=QgsProcessing.TypeRaster,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.RASTER_BAND,
                self.tr('Raster band'),
                type=QgsProcessingParameterNumber.Integer,
                minValue=1,
                defaultValue=1,
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.CONTOUR_LAYER_1,
                self.tr('Contour layer 1 (optional)'),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CONTOUR_DEPTH_FIELD_1,
                self.tr('Depth field (contour layer 1)'),
                parentLayerParameterName=self.CONTOUR_LAYER_1,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.CONTOUR_LAYER_2,
                self.tr('Contour layer 2 (optional)'),
                [QgsProcessing.TypeVectorLine],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.CONTOUR_DEPTH_FIELD_2,
                self.tr('Depth field (contour layer 2)'),
                parentLayerParameterName=self.CONTOUR_LAYER_2,
                type=QgsProcessingParameterField.Numeric,
                optional=True,
            )
        )

        self.addParameter(
            QgsProcessingParameterNumber(
                self.CONTOUR_SEARCH_RADIUS_M,
                self.tr('Contour search radius (m, 0 = unlimited)'),
                type=QgsProcessingParameterNumber.Double,
                minValue=0.0,
                defaultValue=0.0,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_ABS_DEPTH,
                self.tr('Use absolute depth values'),
                defaultValue=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterEnum(
                self.OUTPUT_MODE,
                self.tr('Output fields'),
                options=[self.tr('Best depth only'), self.tr('Best + per-raster depth fields')],
                defaultValue=0,
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output point layer'),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT, context)
        if not source:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))

        depth_source_mode = int(self.parameterAsEnum(parameters, self.DEPTH_SOURCE, context) or 0)
        use_abs = bool(self.parameterAsBool(parameters, self.USE_ABS_DEPTH, context))
        output_mode = int(self.parameterAsEnum(parameters, self.OUTPUT_MODE, context) or 0)

        raster_layers = [
            lyr
            for lyr in (self.parameterAsLayerList(parameters, self.RASTER_LAYERS, context) or [])
            if isinstance(lyr, QgsRasterLayer)
        ]
        raster_band = int(self.parameterAsInt(parameters, self.RASTER_BAND, context) or 1)

        contour_layer_1 = self.parameterAsVectorLayer(parameters, self.CONTOUR_LAYER_1, context)
        contour_depth_field_1 = (self.parameterAsString(parameters, self.CONTOUR_DEPTH_FIELD_1, context) or '').strip()
        contour_layer_2 = self.parameterAsVectorLayer(parameters, self.CONTOUR_LAYER_2, context)
        contour_depth_field_2 = (self.parameterAsString(parameters, self.CONTOUR_DEPTH_FIELD_2, context) or '').strip()
        contour_search_radius_m = float(self.parameterAsDouble(parameters, self.CONTOUR_SEARCH_RADIUS_M, context) or 0.0)

        # Validate sources
        want_raster = depth_source_mode in (0, 1)
        want_contours = depth_source_mode in (0, 2)
        if depth_source_mode == 1:
            want_contours = False
        if depth_source_mode == 2:
            want_raster = False

        if want_raster and not raster_layers:
            feedback.pushWarning(self.tr('No raster layers provided; raster sampling will be skipped'))
        if want_contours and not contour_layer_1 and not contour_layer_2:
            feedback.pushWarning(self.tr('No contour layers provided; contour sampling will be skipped'))
        if want_raster and not raster_layers and (not want_contours or (not contour_layer_1 and not contour_layer_2)):
            # Raster-only with no rasters, or auto with neither raster nor contours
            raise QgsProcessingException(self.tr('No depth sources provided. Add at least one raster or a contour layer.'))

        points_crs = source.sourceCrs()

        raster_samplers = self._build_raster_samplers(raster_layers, points_crs)
        contour_samplers = self._build_contour_samplers(
            [contour_layer_1, contour_layer_2],
            [contour_depth_field_1, contour_depth_field_2],
            points_crs,
        )

        used_field_names = set([f.name() for f in source.fields()])

        # Output fields: copy input + depth fields
        out_fields = QgsFields(source.fields())
        out_fields.append(QgsField('depth', QVariant.Double))
        out_fields.append(QgsField('depth_source', QVariant.String))
        out_fields.append(QgsField('depth_contour_dist_m', QVariant.Double))

        raster_field_map: List[Tuple[str, Tuple[QgsRasterLayer, Optional[QgsCoordinateTransform]]]] = []
        if output_mode == 1 and raster_samplers:
            for raster, transform in raster_samplers:
                fld = self._safe_field_name(f"depth_{raster.name()}", used_field_names)
                out_fields.append(QgsField(fld, QVariant.Double))
                raster_field_map.append((fld, (raster, transform)))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            out_fields,
            QgsWkbTypes.Point,
            source.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        features = list(source.getFeatures())
        total = len(features) if features else 1
        added = 0

        for idx, f in enumerate(features):
            if feedback.isCanceled():
                break

            feedback.setProgress(int((idx / total) * 100))

            geom = f.geometry()
            if not geom or geom.isEmpty():
                continue

            # Extract a representative point.
            point = None
            try:
                if geom.isMultipart():
                    pts = geom.asMultiPoint()
                    if pts:
                        point = QgsPointXY(pts[0])
                else:
                    pt = geom.asPoint()
                    point = QgsPointXY(pt)
            except Exception:
                point = None

            if point is None:
                try:
                    p = geom.pointOnSurface().asPoint()
                    point = QgsPointXY(p)
                except Exception:
                    continue

            raster_best = None
            raster_src = None
            raster_all: List[Tuple[str, Optional[float]]] = []
            if want_raster and raster_samplers:
                raster_best, raster_src, raster_all = self._sample_rasters(point, raster_samplers, raster_band)

            contour_best = None
            contour_src = None
            contour_dist = None
            if want_contours and contour_samplers:
                contour_best, contour_src, contour_dist = self._sample_contours(
                    point,
                    contour_samplers,
                    contour_search_radius_m,
                    context,
                )

            # Choose final depth
            depth_val = None
            depth_source = None
            depth_contour_dist_m = None

            if depth_source_mode == 1:
                depth_val = raster_best
                depth_source = raster_src
            elif depth_source_mode == 2:
                depth_val = contour_best
                depth_source = contour_src
                depth_contour_dist_m = contour_dist
            else:
                if raster_best is not None:
                    depth_val = raster_best
                    depth_source = raster_src
                else:
                    depth_val = contour_best
                    depth_source = contour_src
                    depth_contour_dist_m = contour_dist

            if depth_val is not None and use_abs:
                try:
                    depth_val = abs(float(depth_val))
                except Exception:
                    pass

            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(geom)

            attrs = list(f.attributes())
            attrs.extend([depth_val, depth_source, depth_contour_dist_m])

            if output_mode == 1 and raster_field_map:
                # Map raster names to values so we can fill per-raster fields.
                raster_values_by_name = {name: val for name, val in raster_all}
                for fld, (raster, _transform) in raster_field_map:
                    attrs.append(raster_values_by_name.get(raster.name()))

            out_feat.setAttributes(attrs)
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
            added += 1

        feedback.pushInfo(self.tr(f'Updated {added} points.'))
        return {self.OUTPUT: dest_id}

    def name(self):
        return 'add_depth_to_points'

    def displayName(self):
        return self.tr('Add Depth to Point Layer')

    def group(self):
        return self.tr('KP Points')

    def groupId(self):
        return 'kppoints'

    def shortHelpString(self):
        return self.tr(
            """
Adds depth/elevation values to a point layer.

Depth can be sampled from one or more raster layers (using the chosen band), and/or derived from the nearest contour feature.

- **Auto**: tries rasters in order first, then falls back to contours.
- **Raster only**: uses raster sampling only.
- **Contours only**: uses nearest contour only (optionally limited by search radius).

If **Best + per-raster depth fields** is enabled, the output will also include one field per raster layer.
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('AddDepthToPointLayerAlgorithm', string)

    def createInstance(self):
        return AddDepthToPointLayerAlgorithm()
