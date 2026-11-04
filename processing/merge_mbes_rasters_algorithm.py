# -*- coding: utf-8 -*-
"""Merge MBES rasters into one mosaic at the finest input resolution.

Built on a GDAL virtual raster (gdalbuildvrt -resolution highest) rather
than gdal_merge, which silently resamples every input to the FIRST file's
resolution — merging a 0.25 m grid into a 1 m grid used to throw the fine
detail away depending on selection order. The VRT is then materialised to
a tiled GeoTIFF, so depth values pass through untouched.
"""

from qgis.PyQt.QtCore import QCoreApplication
import json
import math
import os
import uuid
from qgis.core import (
    QgsApplication,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterRasterDestination,
    QgsRasterLayer,
    QgsRectangle,
)
from .algorithm_base import SubseaCableAlgorithm, gdal_creation_options
from ..qgis_compat import PROCESSING_SOURCE_RASTER, processing_temp_folder
from qgis import processing


class MergeMBESRastersAlgorithm(SubseaCableAlgorithm):
    """Mosaic MBES rasters, preserving the finest resolution and NoData."""

    INPUTS = 'INPUTS'
    OUTPUT = 'OUTPUT'
    COMPRESS = 'COMPRESS'
    RESAMPLING = 'RESAMPLING'
    VIRTUAL = 'VIRTUAL'
    ALLOW_LARGE = 'ALLOW_LARGE'

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUTS,
                self.tr('Input MBES Raster Layers'),
                layerType=PROCESSING_SOURCE_RASTER
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.RESAMPLING,
                self.tr('Resampling of coarser inputs to the finest grid'),
                options=[
                    self.tr('Nearest (values pass through untouched, blocky)'),
                    self.tr('Bilinear (smooth surface, values interpolated)'),
                ],
                defaultValue=0,
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

        self.addParameter(QgsProcessingParameterBoolean(
            self.VIRTUAL, self.tr('Create a virtual mosaic (.vrt) referencing the original files'), defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ALLOW_LARGE, self.tr('Allow a physical mosaic exceeding one billion cells'), defaultValue=False))

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
        resampling = int(self.parameterAsEnum(parameters, self.RESAMPLING, context))
        virtual = self.parameterAsBool(parameters, self.VIRTUAL, context)
        if virtual and not final_output.lower().endswith('.vrt'):
            # QGIS's temporary raster destination normally has a .tif suffix.
            if str(parameters.get(self.OUTPUT, '')).upper() == 'TEMPORARY_OUTPUT':
                final_output = os.path.splitext(final_output)[0] + '.vrt'
            else:
                raise QgsProcessingException('For a virtual mosaic, choose an output filename ending in .vrt.')
        if not virtual and final_output.lower().endswith('.vrt'):
            raise QgsProcessingException('Enable Create a virtual mosaic when saving to .vrt.')

        # --- validate inputs: files, one CRS, note resolutions and NoData ---
        input_files = []
        crs_by_layer = {}
        finest = None
        finest_x = finest_y = None
        bounds = QgsRectangle()
        valid_layers = []
        coarsest = None
        for lyr in raster_layers:
            if not isinstance(lyr, QgsRasterLayer) or not lyr.isValid():
                feedback.pushWarning(f'Skipping invalid input: {lyr.name()} is not a valid raster layer.')
                continue
            source = lyr.source().split('|')[0]
            input_files.append(source)
            valid_layers.append(lyr)
            bounds.combineExtentWith(lyr.extent())
            crs_by_layer[lyr.name()] = lyr.crs().authid() or lyr.crs().toWkt()
            res_x = abs(lyr.rasterUnitsPerPixelX())
            res_y = abs(lyr.rasterUnitsPerPixelY())
            if res_x <= 0 or res_y <= 0:
                raise QgsProcessingException(f'{lyr.name()} has no usable native pixel size.')
            finest_x = min(finest_x, res_x) if finest_x is not None else res_x
            finest_y = min(finest_y, res_y) if finest_y is not None else res_y
            finest = min(finest, res_x, res_y) if finest is not None else min(res_x, res_y)
            coarsest = max(coarsest, res_x, res_y) if coarsest is not None else max(res_x, res_y)
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

        cells = math.ceil(bounds.width() / finest_x) * math.ceil(bounds.height() / finest_y)
        feedback.pushInfo(f'Full mosaic grid: approximately {cells:,} cells including gaps '
                          f'({cells * 4 / 1024 ** 3:.1f} GiB for one uncompressed Float32 band).')
        if not virtual and cells > 1_000_000_000 and not self.parameterAsBool(parameters, self.ALLOW_LARGE, context):
            raise QgsProcessingException(
                'The finest-resolution mosaic exceeds one billion cells. Use a virtual mosaic (.vrt), '
                'sample the source rasters directly in Burial Planner, or explicitly enable Allow a physical '
                'mosaic exceeding one billion cells after checking disk space.')
        if feedback.isCanceled():
            return {}
        feedback.pushInfo(
            f'Merging {len(input_files)} rasters at the finest input resolution '
            f'({finest:g} units/pixel). Where tiles overlap, later-listed inputs win.')
        if coarsest is not None and finest is not None and coarsest > finest * 1.001:
            feedback.pushWarning(
                f'Mixed resolutions ({finest:g}–{coarsest:g} units/pixel): '
                'coarser tiles are upsampled to the finest grid '
                f'({"bilinear interpolation" if resampling == 1 else "nearest-neighbour blocks"}). '
                'The upsampled cells carry no new information and the mosaic '
                'does not store per-cell source provenance. This plugin records '
                'native source paths in a sidecar for engineering profiles; '
                'other tools reading only the TIFF use the upsampled surface. Overlapping tiles are not checked for vertical-'
                'datum agreement; verify overlaps before relying on seam areas.')

        # --- build the VRT mosaic at the highest resolution ---
        vrt_path = final_output if virtual else os.path.join(
            processing_temp_folder(context), f'mbes_merge_{uuid.uuid4().hex[:8]}.vrt')
        result = processing.run('gdal:buildvirtualraster', {
            'INPUT': input_files,
            'RESOLUTION': 1,          # highest — never degrade a fine grid
            'SEPARATE': False,
            'PROJ_DIFFERENCE': False,
            'ADD_ALPHA': False,
            'ASSIGN_CRS': None,
            # 0 nearest (values pass through untouched), 1 bilinear (coarser
            # inputs upsampled smoothly instead of as blocks).
            'RESAMPLING': 1 if resampling == 1 else 0,
            'SRC_NODATA': None,       # respect each file's own NoData metadata
            'OUTPUT': vrt_path,
        }, context=context, feedback=feedback, is_child_algorithm=True)
        if not result or not result.get('OUTPUT'):
            raise QgsProcessingException('Raster merge (buildvirtualraster) failed. '
                                         'Check the processing log for more details.')

        if feedback.isCanceled():
            return {}
        if virtual:
            result2 = result
        else:
            # --- materialise to GeoTIFF ---
            options = 'COMPRESS=LZW|TILED=YES|BIGTIFF=IF_SAFER' if compress else 'TILED=YES|BIGTIFF=IF_SAFER'
            result2 = processing.run('gdal:translate', {
                'INPUT': result['OUTPUT'],
                'OUTPUT': final_output,
                **gdal_creation_options('gdal:translate', options),
                'EXTRA': '--config GDAL_CACHEMAX 256',
                'DATA_TYPE': 0,           # keep the source data type
            }, context=context, feedback=feedback, is_child_algorithm=True)
            if not result2 or not result2.get('OUTPUT'):
                raise QgsProcessingException('Writing the merged raster (gdal:translate) failed. '
                                             'Check the processing log for more details.')
            try:
                os.remove(vrt_path)
            except OSError:
                pass
        from ..bathymetry_sampling import MANIFEST_VERSION, layer_options, expand_rasters
        native = expand_rasters(valid_layers)
        manifest_path = str(result2['OUTPUT']) + '.sources.json'
        with open(manifest_path, 'w', encoding='utf-8') as stream:
            json.dump({'version': MANIFEST_VERSION, 'analysis_priority': 'finest native first',
                       'sources': [{'path': os.path.abspath(layer.source().split('|')[0]),
                                    'name': layer.name(), 'options': layer_options(layer)}
                                   for layer in native]}, stream, indent=2)
        feedback.pushInfo('Engineering profiles read the native sources recorded in ' + manifest_path)
        feedback.pushInfo('Keep the native source rasters and the .sources.json sidecar with this result. '
                          'Temporary processing files may be deleted; save source grids permanently '
                          'and rerun the merge for a reusable result. Profile overlaps use finest native '
                          'resolution first, which can differ from the displayed mosaic overlap order.')
        feedback.pushInfo('Virtual mosaic created; keep the source files at their current paths.' if virtual else
                          'Merge complete. Output pixel size matches the finest input; '
                          'coarser areas retain their original information resolution.')
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
<p>Merges multiple MBES raster layers into a single mosaic <b>at the finest input resolution</b> — a 0.25&nbsp;m tile merged with 1&nbsp;m tiles keeps its 0.25&nbsp;m detail regardless of selection order. With <i>Nearest</i> resampling (default) depth values pass through untouched; <i>Bilinear</i> upsamples coarser tiles smoothly instead of as blocks (interpolated values, no new information).</p>
<ul>
  <li>Select two or more raster layers (e.g. from <i>Create Raster from XYZ</i>).</li>
  <li>All inputs must share one CRS; mixed CRS is refused with a clear message.</li>
  <li>Each file's own NoData value is respected, so gaps stay transparent; where tiles overlap, later-listed inputs take priority.</li>
  <li>Choose a virtual mosaic and a .vrt filename for large collections: source files stay separate and pixels are read on demand. Keep the source files at their current paths. This can be processed by GDAL/QGIS like a raster, but a whole-grid operation still processes the full grid.</li>
  <li>Physical output is a tiled GeoTIFF; LZW compression is lossless. Large grids require explicit opt-in; the estimated cell count includes gaps between surveys.</li>
  <li>For fast display of large mosaics, build pyramids afterwards (right-click the layer → Properties → Pyramids, or gdaladdo).</li>
</ul>
<p><b>Engineering note:</b> the mosaic is a display/profiling surface. Upsampled cells from coarser tiles carry no extra information, per-cell source provenance is lost, and overlapping tiles are not checked for vertical-datum agreement — refer engineering analyses (and seam checks) to the source grids, which the profile tools can consume directly as multiple layers.</p>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)
