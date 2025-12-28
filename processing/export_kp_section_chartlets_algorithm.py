# export_kp_section_chartlets_algorithm.py
# -*- coding: utf-8 -*-
"""ExportKPSectionChartletsAlgorithm

Exports per-feature map images ("chartlets") centered on route/KP sections.

Typical workflow:
- Use KPRangeCSVAlgorithm (or your own process) to create a KP-range line layer
  with fields like start_kp/end_kp.
- Run this algorithm to export a PNG per range/segment.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QSize
from qgis.core import (
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsMapLayer,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterDistance,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsRectangle,
    QgsUnitTypes,
    QgsWkbTypes,
    QgsDistanceArea,
    QgsPrintLayout,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLayoutItemPicture,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLayoutExporter,
)

from ..kp_range_utils import extract_line_segment, measure_total_length_m


def _safe_filename(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-_") or "chartlet"


def _expand_extent_to_aspect(extent: QgsRectangle, width_px: int, height_px: int) -> QgsRectangle:
    if width_px <= 0 or height_px <= 0:
        return extent

    target_ratio = float(width_px) / float(height_px)
    w = extent.width()
    h = extent.height()
    if w <= 0 or h <= 0:
        return extent

    current_ratio = w / h
    if abs(current_ratio - target_ratio) < 1e-9:
        return extent

    cx = extent.center().x()
    cy = extent.center().y()

    if current_ratio > target_ratio:
        # extent too wide -> expand height
        new_h = w / target_ratio
        half_h = new_h / 2.0
        return QgsRectangle(extent.xMinimum(), cy - half_h, extent.xMaximum(), cy + half_h)
    else:
        # extent too tall -> expand width
        new_w = h * target_ratio
        half_w = new_w / 2.0
        return QgsRectangle(cx - half_w, extent.yMinimum(), cx + half_w, extent.yMaximum())


class ExportKPSectionChartletsAlgorithm(QgsProcessingAlgorithm):
    RANGES = 'RANGES'
    START_KP_FIELD = 'START_KP_FIELD'
    END_KP_FIELD = 'END_KP_FIELD'
    RPL_LINE = 'RPL_LINE'
    EXTRA_ALONG_KM = 'EXTRA_ALONG_KM'
    EXTRA_MARGIN = 'EXTRA_MARGIN'
    USE_PROJECT_LAYERS = 'USE_PROJECT_LAYERS'
    VECTOR_LAYERS = 'VECTOR_LAYERS'
    RASTER_LAYERS = 'RASTER_LAYERS'
    OUT_FOLDER = 'OUT_FOLDER'
    IMAGE_WIDTH = 'IMAGE_WIDTH'
    IMAGE_HEIGHT = 'IMAGE_HEIGHT'
    FILE_PREFIX = 'FILE_PREFIX'
    ADD_SCALEBAR = 'ADD_SCALEBAR'
    ADD_NORTH_ARROW = 'ADD_NORTH_ARROW'

    def tr(self, string: str) -> str:
        return QCoreApplication.translate('ExportKPSectionChartletsAlgorithm', string)

    def name(self) -> str:
        return 'export_kp_section_chartlets'

    def displayName(self) -> str:
        return self.tr('Export KP section chartlets')

    def group(self) -> str:
        return self.tr('Other Tools')

    def groupId(self) -> str:
        return 'other_tools'

    def shortHelpString(self) -> str:
        return self.tr(
            """Exports a PNG per KP range/segment, with the map view centered and zoomed to fit each section.

You can run it in two modes:

1) **Segment geometry mode** (no RPL line)
   - Input RANGES layer contains line geometries for each section.
   - Extent is based on each feature's geometry.

2) **KP extraction mode** (with RPL line)
   - Input RANGES is a table or layer with numeric start/end KP fields.
   - Input RPL_LINE is the reference route line.
   - The algorithm extracts the KP segment from the RPL line and uses that to set the extent.

Parameters:
- **Extra distance along route (km)** expands the KP range in both directions when using KP extraction mode.
- **Extra map margin** adds padding around the final extent (in the map units of the chosen destination CRS).

Output:
- One PNG per feature saved to the chosen folder.
"""
        )

    def createInstance(self):
        return ExportKPSectionChartletsAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.RANGES,
                self.tr('Input KP ranges (segment layer or table)'),
                [QgsProcessing.TypeVector]
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.RPL_LINE,
                self.tr('Reference RPL line layer (optional)'),
                [QgsProcessing.TypeVectorLine],
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.START_KP_FIELD,
                self.tr('Start KP field (used with Reference RPL line)'),
                parentLayerParameterName=self.RANGES,
                type=QgsProcessingParameterField.Numeric,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.END_KP_FIELD,
                self.tr('End KP field (used with Reference RPL line)'),
                parentLayerParameterName=self.RANGES,
                type=QgsProcessingParameterField.Numeric,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.EXTRA_ALONG_KM,
                self.tr('Extra distance along route (km)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterDistance(
                self.EXTRA_MARGIN,
                self.tr('Extra map margin (map units)'),
                defaultValue=0.0,
                parentParameterName=self.RANGES
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_PROJECT_LAYERS,
                self.tr('Use visible project layers (recommended)'),
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.RASTER_LAYERS,
                self.tr('Raster layers to render (ignored if using visible project layers)'),
                layerType=QgsProcessing.TypeRaster,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.VECTOR_LAYERS,
                self.tr('Vector layers to render (ignored if using visible project layers)'),
                layerType=QgsProcessing.TypeVector,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUT_FOLDER,
                self.tr('Output folder')
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.IMAGE_WIDTH,
                self.tr('Image width (px)'),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=1200,
                minValue=64
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.IMAGE_HEIGHT,
                self.tr('Image height (px)'),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=800,
                minValue=64
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FILE_PREFIX,
                self.tr('Filename prefix (optional)'),
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_SCALEBAR,
                self.tr('Add scale bar (bottom-left)'),
                defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_NORTH_ARROW,
                self.tr('Add north arrow (top-right)'),
                defaultValue=True
            )
        )

    def _resolve_dest_crs(
        self,
        ranges_source,
        rpl_source,
        context
    ) -> QgsCoordinateReferenceSystem:
        project = context.project()
        if project is not None:
            crs = project.crs()
            if crs is not None and crs.isValid():
                return crs

        # Fallbacks
        if rpl_source is not None:
            return rpl_source.sourceCrs()
        return ranges_source.sourceCrs()

    def _project_visible_layers_in_order(self, context) -> List[QgsMapLayer]:
        project = context.project()
        if project is None:
            return []
        root = project.layerTreeRoot()
        # checkedLayers() returns layers respecting tree order
        layers = root.checkedLayers() if root is not None else []
        return [lyr for lyr in layers if isinstance(lyr, QgsMapLayer) and lyr.isValid()]

    def _sort_layers_by_project_order(self, layers: List[QgsMapLayer], context) -> List[QgsMapLayer]:
        if not layers:
            return []
        project_layers = self._project_visible_layers_in_order(context)
        order = {lyr.id(): idx for idx, lyr in enumerate(project_layers)}

        def key(lyr: QgsMapLayer):
            return order.get(lyr.id(), 10**9)

        return sorted(layers, key=key)

    def _transform_geometry(self, geom: QgsGeometry, source_crs: QgsCoordinateReferenceSystem, dest_crs: QgsCoordinateReferenceSystem, context) -> QgsGeometry:
        if geom is None or geom.isEmpty() or not source_crs.isValid() or not dest_crs.isValid() or source_crs == dest_crs:
            return geom
        g = QgsGeometry(geom)
        xform = QgsCoordinateTransform(source_crs, dest_crs, context.transformContext())
        g.transform(xform)
        return g

    def _dissolve_rpl_geometry(self, rpl_source, context, feedback) -> Optional[QgsGeometry]:
        geometries = [f.geometry() for f in rpl_source.getFeatures()]
        if not geometries:
            return None
        combined = QgsGeometry.unaryUnion(geometries)
        if combined is None or combined.isEmpty():
            return None
        return combined

    def _kp_fields_or_guess(self, ranges_source, start_field: str, end_field: str) -> Tuple[Optional[str], Optional[str]]:
        if start_field and end_field:
            return start_field, end_field

        names = {n.lower(): n for n in ranges_source.fields().names()}
        guessed_start = names.get('start_kp') or names.get('kp_from') or names.get('kp_start')
        guessed_end = names.get('end_kp') or names.get('kp_to') or names.get('kp_end')
        return guessed_start, guessed_end

    def _feature_kp_range(self, feature: QgsFeature, start_field: str, end_field: str) -> Optional[Tuple[float, float]]:
        try:
            start_val = feature[start_field]
            end_val = feature[end_field]
        except Exception:
            return None

        if start_val is None or end_val is None:
            return None
        try:
            start_kp = float(start_val)
            end_kp = float(end_val)
        except Exception:
            return None
        if end_kp < start_kp:
            start_kp, end_kp = end_kp, start_kp
        return start_kp, end_kp

    def _make_output_name(
        self,
        prefix: str,
        feature: QgsFeature,
        start_kp: Optional[float],
        end_kp: Optional[float]
    ) -> str:
        base = prefix or 'kp_section'
        if start_kp is not None and end_kp is not None:
            label = f"{start_kp:g}-{end_kp:g}"
        else:
            label = f"fid_{feature.id()}"
        return _safe_filename(f"{base}_{label}.png")

    def processAlgorithm(self, parameters, context, feedback):
        ranges_source = self.parameterAsSource(parameters, self.RANGES, context)
        if ranges_source is None:
            raise QgsProcessingException(self.tr('Invalid input KP ranges layer/table.'))

        rpl_source = self.parameterAsSource(parameters, self.RPL_LINE, context)
        start_field_raw = self.parameterAsString(parameters, self.START_KP_FIELD, context)
        end_field_raw = self.parameterAsString(parameters, self.END_KP_FIELD, context)
        extra_along_km = float(self.parameterAsDouble(parameters, self.EXTRA_ALONG_KM, context) or 0.0)
        extra_margin = float(self.parameterAsDouble(parameters, self.EXTRA_MARGIN, context) or 0.0)

        use_project_layers = bool(self.parameterAsBool(parameters, self.USE_PROJECT_LAYERS, context))
        add_scalebar = bool(self.parameterAsBool(parameters, self.ADD_SCALEBAR, context))
        add_north_arrow = bool(self.parameterAsBool(parameters, self.ADD_NORTH_ARROW, context))

        width_px = int(self.parameterAsInt(parameters, self.IMAGE_WIDTH, context))
        height_px = int(self.parameterAsInt(parameters, self.IMAGE_HEIGHT, context))
        file_prefix = (self.parameterAsString(parameters, self.FILE_PREFIX, context) or '').strip()

        out_folder = self.parameterAsString(parameters, self.OUT_FOLDER, context)
        if not out_folder:
            raise QgsProcessingException(self.tr('Output folder is required.'))
        os.makedirs(out_folder, exist_ok=True)

        layers: List[QgsMapLayer] = []
        if use_project_layers:
            layers = self._project_visible_layers_in_order(context)
            if not layers:
                raise QgsProcessingException(self.tr('No visible layers found in the current project.'))
        else:
            raster_layers: List[QgsMapLayer] = self.parameterAsLayerList(parameters, self.RASTER_LAYERS, context) or []
            vector_layers: List[QgsMapLayer] = self.parameterAsLayerList(parameters, self.VECTOR_LAYERS, context) or []
            layers = [lyr for lyr in (raster_layers + vector_layers) if isinstance(lyr, QgsMapLayer) and lyr.isValid()]
            layers = self._sort_layers_by_project_order(layers, context)
            if not layers:
                raise QgsProcessingException(self.tr('Please choose at least one raster or vector layer to render, or enable "Use visible project layers".'))

        dest_crs = self._resolve_dest_crs(ranges_source, rpl_source, context)
        if dest_crs.isGeographic():
            feedback.pushInfo(
                self.tr(
                    'Warning: destination CRS is geographic (degrees). Extra map margin is in degrees; consider using a projected CRS for consistent distances.'
                )
            )

        rpl_geom = None
        distance_calculator = None
        total_length_km = None
        if rpl_source is not None:
            rpl_geom = self._dissolve_rpl_geometry(rpl_source, context, feedback)
            if rpl_geom is None:
                raise QgsProcessingException(self.tr('Reference RPL line layer has no valid geometry.'))

            distance_calculator = QgsDistanceArea()
            distance_calculator.setSourceCrs(rpl_source.sourceCrs(), context.transformContext())
            distance_calculator.setEllipsoid(context.project().ellipsoid())
            total_length_m = float(measure_total_length_m(rpl_geom, distance_calculator))
            total_length_km = total_length_m / 1000.0

        start_field, end_field = self._kp_fields_or_guess(ranges_source, start_field_raw, end_field_raw)

        use_kp_extraction = rpl_source is not None
        if use_kp_extraction and (not start_field or not end_field):
            raise QgsProcessingException(
                self.tr('Start/End KP fields are required when Reference RPL line is provided (or use fields named start_kp/end_kp).')
            )

        features = list(ranges_source.getFeatures())
        total = len(features)
        if total == 0:
            feedback.pushInfo(self.tr('No features found in KP ranges input.'))
            return {}

        exported = 0
        skipped = 0

        # Build a layout template once; we will only update extent and export path per feature.
        project = context.project()
        if project is None:
            raise QgsProcessingException(self.tr('No active QGIS project found in processing context.'))

        layout = QgsPrintLayout(project)
        layout.initializeDefaults()

        # Set a deterministic page size matching the requested aspect ratio.
        width_mm = 200.0
        height_mm = width_mm * (float(height_px) / float(width_px))
        pc = layout.pageCollection()
        if pc.pageCount() > 0:
            pc.page(0).setPageSize(QgsLayoutSize(width_mm, height_mm, QgsUnitTypes.LayoutMillimeters))

        map_item = QgsLayoutItemMap(layout)
        map_item.setCrs(dest_crs)
        map_item.setLayers(layers)
        map_item.attemptResize(QgsLayoutSize(width_mm, height_mm, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptMove(QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(map_item)

        scalebar_item = None
        if add_scalebar:
            scalebar_item = QgsLayoutItemScaleBar(layout)
            scalebar_item.setLinkedMap(map_item)
            scalebar_item.applyDefaultSize()
            scalebar_item.attemptMove(QgsLayoutPoint(5, height_mm - 15, QgsUnitTypes.LayoutMillimeters))
            layout.addLayoutItem(scalebar_item)

        north_item = None
        if add_north_arrow:
            north_item = QgsLayoutItemPicture(layout)
            # QGIS built-in north arrow SVG (resource path)
            north_item.setPicturePath(':/images/north_arrows/layout_default_north_arrow.svg')
            north_item.attemptResize(QgsLayoutSize(15, 15, QgsUnitTypes.LayoutMillimeters))
            north_item.attemptMove(QgsLayoutPoint(width_mm - 20, 5, QgsUnitTypes.LayoutMillimeters))
            layout.addLayoutItem(north_item)

        for i, feature in enumerate(features):
            if feedback.isCanceled():
                break

            start_kp = None
            end_kp = None
            geom_for_extent: Optional[QgsGeometry] = None

            if use_kp_extraction:
                kp_range = self._feature_kp_range(feature, start_field, end_field)
                if kp_range is None:
                    skipped += 1
                    continue
                start_kp, end_kp = kp_range
                start_kp = max(0.0, start_kp - extra_along_km)
                end_kp = end_kp + extra_along_km
                if total_length_km is not None:
                    end_kp = min(total_length_km, end_kp)

                seg_geom = extract_line_segment(rpl_geom, start_kp, end_kp, distance_calculator)
                if seg_geom is None or seg_geom.isEmpty():
                    skipped += 1
                    continue
                geom_for_extent = seg_geom
            else:
                if not ranges_source.hasGeometry():
                    raise QgsProcessingException(
                        self.tr('KP ranges input has no geometry. Provide a Reference RPL line + KP fields, or use a line segment layer as input.')
                    )
                g = feature.geometry()
                if g is None or g.isEmpty():
                    skipped += 1
                    continue
                geom_for_extent = g

            source_crs = rpl_source.sourceCrs() if use_kp_extraction else ranges_source.sourceCrs()
            geom_for_extent = self._transform_geometry(geom_for_extent, source_crs, dest_crs, context)

            extent = geom_for_extent.boundingBox()
            if extra_margin > 0:
                extent.grow(extra_margin)

            # Ensure non-zero extent for very short/point-like geometries
            if extent.width() == 0 and extent.height() == 0:
                # grow by 1 map unit (or degrees) as last resort
                extent.grow(max(1.0, extra_margin))

            extent = _expand_extent_to_aspect(extent, width_px, height_px)

            map_item.setExtent(extent)

            prefix = file_prefix or getattr(ranges_source, 'sourceName', lambda: '')() or 'kp_ranges'
            filename = self._make_output_name(prefix, feature, start_kp, end_kp)
            out_path = os.path.join(out_folder, filename)

            exporter = QgsLayoutExporter(layout)
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.imageSize = QSize(width_px, height_px)
            result = exporter.exportToImage(out_path, settings)
            if result == QgsLayoutExporter.Success:
                exported += 1
            else:
                skipped += 1

            feedback.setProgress(int((i + 1) * 100 / total))

        feedback.pushInfo(self.tr(f'Exported {exported} chartlets. Skipped {skipped}.'))
        return {}
