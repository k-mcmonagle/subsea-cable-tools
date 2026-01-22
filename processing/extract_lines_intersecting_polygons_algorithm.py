# extract_lines_intersecting_polygons_algorithm.py
# -*- coding: utf-8 -*-
"""ExtractLinesIntersectingPolygonsAlgorithm

Creates a single output line layer containing all features from one or more input
line layers that intersect an input polygon layer.

Optionally trims (clips) output geometries to the polygon(s).

CRS handling:
- Intersection/clipping is performed in the polygon layer CRS when valid.
- If a layer is missing/invalid CRS, the algorithm assumes it matches the polygon
  CRS (or the output CRS if polygon CRS is invalid) and emits a warning.
- Output geometries are written in the user-selected output CRS (default EPSG:4326).

Attributes:
- Output fields are the union of all input line layer fields (with safe de-duplication).
- Adds source reference fields: src_layer_name, src_layer_id, src_fid.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

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
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterMultipleLayers,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)


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


class ExtractLinesIntersectingPolygonsAlgorithm(QgsProcessingAlgorithm):
    INPUT_POLYGONS = 'INPUT_POLYGONS'
    INPUT_LINES = 'INPUT_LINES'
    TRIM_TO_POLYGONS = 'TRIM_TO_POLYGONS'
    OUTPUT_CRS = 'OUTPUT_CRS'
    OUTPUT = 'OUTPUT'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_POLYGONS,
                self.tr('Input polygon layer'),
                [QgsProcessing.TypeVectorPolygon],
            )
        )

        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_LINES,
                self.tr('Input line layer(s)'),
                layerType=QgsProcessing.TypeVectorLine,
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.TRIM_TO_POLYGONS,
                self.tr('Trim (clip) output geometries to polygon(s)'),
                defaultValue=False,
            )
        )

        self.addParameter(
            QgsProcessingParameterCrs(
                self.OUTPUT_CRS,
                self.tr('Output CRS'),
                defaultValue='EPSG:4326',
            )
        )

        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr('Output intersecting lines'),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        polygon_layer = self.parameterAsVectorLayer(parameters, self.INPUT_POLYGONS, context)
        line_layers = self.parameterAsLayerList(parameters, self.INPUT_LINES, context) or []
        line_layers = [lyr for lyr in line_layers if isinstance(lyr, QgsVectorLayer)]
        trim = self.parameterAsBoolean(parameters, self.TRIM_TO_POLYGONS, context)

        out_crs = self.parameterAsCrs(parameters, self.OUTPUT_CRS, context)
        if out_crs is None or not out_crs.isValid():
            out_crs = QgsCoordinateReferenceSystem('EPSG:4326')

        if polygon_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT_POLYGONS))
        if not line_layers:
            raise QgsProcessingException(self.tr('Please provide one or more input line layers.'))

        if QgsWkbTypes.geometryType(polygon_layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
            raise QgsProcessingException(self.tr('Input polygon layer must be a polygon layer.'))

        for lyr in line_layers:
            if QgsWkbTypes.geometryType(lyr.wkbType()) != QgsWkbTypes.LineGeometry:
                raise QgsProcessingException(self.tr(f"Layer '{lyr.name()}' is not a line layer."))

        # Choose a working CRS for intersection/clipping.
        poly_crs = polygon_layer.sourceCrs()
        if poly_crs is None or not poly_crs.isValid():
            feedback.pushWarning(
                self.tr(
                    "Polygon layer '{name}' has no valid CRS; performing intersection in output CRS ({crs})."
                ).format(name=polygon_layer.name(), crs=out_crs.authid())
            )
            work_crs = out_crs
        else:
            work_crs = poly_crs

        def _layer_crs_or_fallback(layer: QgsVectorLayer, fallback: QgsCoordinateReferenceSystem) -> QgsCoordinateReferenceSystem:
            layer_crs = layer.sourceCrs()
            if layer_crs is None or not layer_crs.isValid():
                feedback.pushWarning(
                    self.tr(
                        "Layer '{name}' has no valid CRS; assuming it matches {crs}."
                    ).format(name=layer.name(), crs=fallback.authid())
                )
                return fallback
            return layer_crs

        # Build union polygon geometry in work CRS
        poly_geoms_work: List[QgsGeometry] = []

        poly_to_work: Optional[QgsCoordinateTransform]
        if poly_crs is not None and poly_crs.isValid() and poly_crs != work_crs:
            try:
                poly_to_work = QgsCoordinateTransform(poly_crs, work_crs, context.transformContext())
            except Exception:
                poly_to_work = QgsCoordinateTransform(poly_crs, work_crs, QgsProject.instance())
        else:
            poly_to_work = None

        for feat in polygon_layer.getFeatures(QgsFeatureRequest().setSubsetOfAttributes([])):
            if feedback.isCanceled():
                break
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            geom_work = QgsGeometry(geom)
            if poly_to_work is not None:
                try:
                    geom_work.transform(poly_to_work)
                except Exception:
                    continue
            poly_geoms_work.append(geom_work)

        if not poly_geoms_work:
            feedback.pushInfo(self.tr('Polygon layer has no valid geometry; output will be empty.'))

        try:
            polygons_union_work = (
                QgsGeometry.unaryUnion(poly_geoms_work) if len(poly_geoms_work) > 1 else (poly_geoms_work[0] if poly_geoms_work else QgsGeometry())
            )
        except Exception:
            polygons_union_work = poly_geoms_work[0] if poly_geoms_work else QgsGeometry()

        # Output fields: union of all input line fields
        out_fields = QgsFields()
        existing_names: Set[str] = set()

        # Map canonical field name -> output index
        field_name_to_out_idx: Dict[str, int] = {}

        # Track per-layer mapping: src field index -> output field index
        layer_field_maps: Dict[str, Dict[int, int]] = {}

        for lyr in line_layers:
            field_map: Dict[int, int] = {}
            for idx in range(lyr.fields().count()):
                fld = lyr.fields().at(idx)
                base_name = _safe_field_base_name(fld.name())
                if base_name in field_name_to_out_idx:
                    field_map[idx] = field_name_to_out_idx[base_name]
                    continue

                out_name = _unique_field_name(existing_names, base_name)
                out_fields.append(QgsField(out_name, fld.type(), fld.typeName(), fld.length(), fld.precision()))
                out_idx = out_fields.count() - 1
                field_name_to_out_idx[base_name] = out_idx
                field_map[idx] = out_idx
            layer_field_maps[lyr.id()] = field_map

        src_layer_name_field = _unique_field_name(existing_names, 'src_layer_name')
        src_layer_id_field = _unique_field_name(existing_names, 'src_layer_id')
        src_fid_field = _unique_field_name(existing_names, 'src_fid')

        out_fields.append(QgsField(src_layer_name_field, QVariant.String, '', 254, 0))
        out_fields.append(QgsField(src_layer_id_field, QVariant.String, '', 254, 0))
        out_fields.append(QgsField(src_fid_field, QVariant.LongLong))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            out_fields,
            QgsWkbTypes.MultiLineString,
            out_crs,
        )

        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        # Prepare transforms
        work_to_out: Optional[QgsCoordinateTransform]
        if work_crs != out_crs:
            try:
                work_to_out = QgsCoordinateTransform(work_crs, out_crs, context.transformContext())
            except Exception:
                work_to_out = QgsCoordinateTransform(work_crs, out_crs, QgsProject.instance())
        else:
            work_to_out = None

        total = sum(int(lyr.featureCount() or 0) for lyr in line_layers)
        done = 0

        def _set_progress():
            if total > 0:
                feedback.setProgress(int(done / total * 100))

        written = 0
        skipped = 0

        for lyr in line_layers:
            if feedback.isCanceled():
                break

            layer_crs = _layer_crs_or_fallback(lyr, work_crs)

            line_to_work: Optional[QgsCoordinateTransform]
            if layer_crs != work_crs:
                try:
                    line_to_work = QgsCoordinateTransform(layer_crs, work_crs, context.transformContext())
                except Exception:
                    line_to_work = QgsCoordinateTransform(layer_crs, work_crs, QgsProject.instance())
            else:
                line_to_work = None

            field_map = layer_field_maps.get(lyr.id(), {})

            for feat in lyr.getFeatures():
                if feedback.isCanceled():
                    break

                done += 1
                if done % 50 == 0:
                    _set_progress()

                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    skipped += 1
                    continue

                geom_work = QgsGeometry(geom)
                if line_to_work is not None:
                    try:
                        geom_work.transform(line_to_work)
                    except Exception:
                        skipped += 1
                        continue

                if polygons_union_work is None or polygons_union_work.isEmpty():
                    # No polygons -> no intersections.
                    continue

                try:
                    if not polygons_union_work.intersects(geom_work):
                        continue
                except Exception:
                    skipped += 1
                    continue

                if trim:
                    try:
                        geom_work = geom_work.intersection(polygons_union_work)
                    except Exception:
                        skipped += 1
                        continue
                    if geom_work is None or geom_work.isEmpty():
                        continue

                # Ensure output geometry is line-like
                try:
                    if QgsWkbTypes.geometryType(geom_work.wkbType()) != QgsWkbTypes.LineGeometry:
                        # Intersection can yield GeometryCollection; try to extract line part.
                        geom_work = geom_work.convertToType(QgsWkbTypes.LineGeometry, False)
                except Exception:
                    pass

                geom_out = QgsGeometry(geom_work)
                if work_to_out is not None:
                    try:
                        geom_out.transform(work_to_out)
                    except Exception:
                        skipped += 1
                        continue

                if geom_out is None or geom_out.isEmpty():
                    continue

                out_feat = QgsFeature(out_fields)
                out_feat.setGeometry(geom_out)

                attrs = [None] * out_fields.count()
                src_attrs = feat.attributes()
                for src_idx, out_idx in field_map.items():
                    if src_idx < len(src_attrs):
                        attrs[out_idx] = src_attrs[src_idx]

                attrs[out_fields.indexFromName(src_layer_name_field)] = lyr.name()
                attrs[out_fields.indexFromName(src_layer_id_field)] = lyr.id()
                attrs[out_fields.indexFromName(src_fid_field)] = int(feat.id())

                out_feat.setAttributes(attrs)
                sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
                written += 1

        _set_progress()

        feedback.pushInfo(self.tr(f'Wrote {written} intersecting line feature(s).'))
        if skipped:
            feedback.pushInfo(self.tr(f'Skipped {skipped} feature(s) due to errors/invalid geometry.'))

        return {self.OUTPUT: dest_id}

    def name(self):
        return 'extract_lines_intersecting_polygons'

    def displayName(self):
        return self.tr('Extract Lines Intersecting Polygons')

    def group(self):
        return self.tr('Other Tools')

    def groupId(self):
        return 'other_tools'

    def shortHelpString(self):
        return self.tr(
            """
Collects all line features from one or more input line layers which intersect the input polygon layer.

**Inputs**
- Polygon layer: polygon(s) to test against.
- Line layer(s): one or more line layers to extract from.
- Trim (clip): if enabled, output geometries are clipped to the polygon(s).
- Output CRS: CRS for the output layer (default EPSG:4326).

**Outputs**
- A single line layer containing all intersecting (and optionally clipped) line features.
- Output attributes are the union of all input line fields, plus:
  - src_layer_name: source layer name
  - src_layer_id: QGIS layer id
  - src_fid: source feature id

**CRS handling**
- Inputs may use different CRSs. Geometries are reprojected on-the-fly for intersection/clipping.
- If an input layer has no valid CRS, it is assumed to match the polygon CRS (or output CRS if polygon CRS is invalid).
"""
        )

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)

    def createInstance(self):
        return ExtractLinesIntersectingPolygonsAlgorithm()
