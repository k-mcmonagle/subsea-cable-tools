# -*- coding: utf-8 -*-
"""Merge MBES rasters into one mosaic at the finest input resolution.

Built on a GDAL virtual raster (gdalbuildvrt -resolution highest) rather
than gdal_merge, which silently resamples every input to the FIRST file's
resolution — merging a 0.25 m grid into a 1 m grid used to throw the fine
detail away depending on selection order. The VRT is then materialised to
a tiled GeoTIFF, so depth values pass through untouched.
"""

from qgis.PyQt.QtCore import QCoreApplication
import os
import uuid
from qgis.core import (
    QgsApplication,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterRasterDestination,
    QgsProcessingUtils,
    QgsRasterLayer,
)
from ..qgis_compat import PROCESSING_SOURCE_RASTER
from qgis import processing


class MergeMBESRastersAlgorithm(QgsProcessingAlgorithm):
    """Mosaic MBES rasters, preserving the finest resolution and NoData."""

    INPUTS = 'INPUTS'
    OUTPUT = 'OUTPUT'
    COMPRESS = 'COMPRESS'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUTS,
                self.tr('Input MBES Raster Layers'),
                layerType=PROCESSING_SOURCE_RASTER
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                self.tr('Output Merged Raster')
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.COMPRESS,
                self.tr('Apply LZW Compression (smaller file size, no data loss)'),
                defaultValue=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        def alg_available(algorithm_id: str) -> bool:
            try:
                return QgsApplication.processingRegistry().algorithmById(algorithm_id) is not None
            except Exception:
                return False

        for required in ('gdal:buildvirtualraster', 'gdal:translate'):
            if not alg_available(required):
                raise QgsProcessingException(
                    f"Required Processing algorithm '{required}' is not available. "
                    'This usually means the GDAL Processing provider is not installed or not enabled.'
                )

        raster_layers = self.parameterAsLayerList(parameters, self.INPUTS, context)
        if not raster_layers or len(raster_layers) < 2:
            raise QgsProcessingException('Please select at least two raster layers to merge.')

        final_output = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        compress = self.parameterAsBool(parameters, self.COMPRESS, context)

        # --- validate inputs: files, one CRS, note resolutions and NoData ---
        input_files = []
        crs_by_layer = {}
        finest = None
        for lyr in raster_layers:
            if not isinstance(lyr, QgsRasterLayer) or not lyr.isValid():
                feedback.pushWarning(f'Skipping invalid input: {lyr.name()} is not a valid raster layer.')
                continue
            source = lyr.source().split('|')[0]
            input_files.append(source)
            crs_by_layer[lyr.name()] = lyr.crs().authid() or lyr.crs().toWkt()
            res_x = abs(lyr.rasterUnitsPerPixelX())
            res_y = abs(lyr.rasterUnitsPerPixelY())
            finest = min(finest, res_x, res_y) if finest is not None else min(res_x, res_y)
            provider = lyr.dataProvider()
            has_nodata = bool(provider and provider.sourceHasNoDataValue(1))
            feedback.pushInfo(
                f'  {lyr.name()}: {res_x:g} x {res_y:g} units/pixel, '
                f'CRS {crs_by_layer[lyr.name()]}, '
                f'NoData {"set" if has_nodata else "NOT set"}')
            if not has_nodata:
                feedback.pushWarning(
                    f'{lyr.name()} has no NoData value; empty areas of this layer '
                    'may merge as 0 instead of transparent.')

        if len(input_files) < 2:
            raise QgsProcessingException('At least two valid raster layers are required to merge.')
        if len(set(crs_by_layer.values())) > 1:
            detail = ', '.join(f'{name}: {crs}' for name, crs in crs_by_layer.items())
            raise QgsProcessingException(
                'All input rasters must share one CRS — merging mixed CRS would '
                f'misplace data. Found: {detail}. Reproject first (gdal:warpreproject).')

        feedback.pushInfo(
            f'Merging {len(input_files)} rasters at the finest input resolution '
            f'({finest:g} units/pixel). Where tiles overlap, later-listed inputs win.')

        # --- build the VRT mosaic at the highest resolution ---
        vrt_path = os.path.join(
            QgsProcessingUtils.tempFolder(), f'mbes_merge_{uuid.uuid4().hex[:8]}.vrt')
        result = processing.run('gdal:buildvirtualraster', {
            'INPUT': input_files,
            'RESOLUTION': 1,          # highest — never degrade a fine grid
            'SEPARATE': False,
            'PROJ_DIFFERENCE': False,
            'ADD_ALPHA': False,
            'ASSIGN_CRS': None,
            'RESAMPLING': 0,          # nearest: depth values pass through untouched
            'SRC_NODATA': None,       # respect each file's own NoData metadata
            'OUTPUT': vrt_path,
        }, context=context, feedback=feedback)
        if not result or not result.get('OUTPUT'):
            raise QgsProcessingException('Raster merge (buildvirtualraster) failed. '
                                         'Check the processing log for more details.')

        # --- materialise to GeoTIFF ---
        options = 'COMPRESS=LZW|TILED=YES|BIGTIFF=IF_SAFER' if compress else 'TILED=YES|BIGTIFF=IF_SAFER'
        result2 = processing.run('gdal:translate', {
            'INPUT': result['OUTPUT'],
            'OUTPUT': final_output,
            'OPTIONS': options,
            'DATA_TYPE': 0,           # keep the source data type
        }, context=context, feedback=feedback)
        if not result2 or not result2.get('OUTPUT'):
            raise QgsProcessingException('Writing the merged raster (gdal:translate) failed. '
                                         'Check the processing log for more details.')
        try:
            os.remove(vrt_path)
        except OSError:
            pass
        feedback.pushInfo('Merge complete. Original depth values and finest resolution preserved.')
        return {self.OUTPUT: result2['OUTPUT']}

    def createInstance(self):
        return MergeMBESRastersAlgorithm()

    def name(self):
        return 'merge_mbes_rasters'

    def displayName(self):
        return self.tr('Merge MBES Rasters')

    def group(self):
        return self.tr('MBES Tools')

    def groupId(self):
        return 'mbestools'

    def shortHelpString(self):
        return self.tr("""
<h3>Merge MBES Rasters</h3>
<p>Merges multiple MBES raster layers into a single mosaic <b>at the finest input resolution</b> — a 0.25&nbsp;m tile merged with 1&nbsp;m tiles keeps its 0.25&nbsp;m detail regardless of selection order. Depth values pass through untouched (nearest resampling, source data type kept).</p>
<ul>
  <li>Select two or more raster layers (e.g. from <i>Create Raster from XYZ</i>).</li>
  <li>All inputs must share one CRS; mixed CRS is refused with a clear message.</li>
  <li>Each file's own NoData value is respected, so gaps stay transparent; where tiles overlap, later-listed inputs take priority.</li>
  <li>The output is a tiled GeoTIFF; LZW compression is lossless.</li>
  <li>For fast display of large mosaics, build pyramids afterwards (right-click the layer → Properties → Pyramids, or gdaladdo).</li>
</ul>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)
