from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterRasterDestination,
    QgsProcessingException,
    QgsRasterLayer
)
from qgis import processing

class MergeMBESRastersAlgorithm(QgsProcessingAlgorithm):
    """
    Merge multiple MBES raster layers into a single raster, preserving Z (depth) values
    and correctly handling NoData values for transparency.
    """
    INPUTS = 'INPUTS'
    OUTPUT = 'OUTPUT'
    COMPRESS = 'COMPRESS'

    def __init__(self):
        super().__init__()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUTS,
                self.tr('Input MBES Raster Layers'),
                layerType=QgsProcessing.TypeRaster
            )
        )
        self.addParameter(
            QgsProcessingParameterRasterDestination(
                self.OUTPUT,
                self.tr('Output Merged Raster')
            )
        )
        from qgis.core import QgsProcessingParameterBoolean
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.COMPRESS,
                self.tr('Apply LZW Compression (smaller file size, no data loss)'),
                defaultValue=True
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        raster_layers = self.parameterAsLayerList(parameters, self.INPUTS, context)
        if not raster_layers or len(raster_layers) < 2:
            raise QgsProcessingException('Please select at least two raster layers to merge.')

        output_raster_path = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        compress = self.parameterAsBool(parameters, self.COMPRESS, context)

        # If compression is enabled, create to a temp file first, then compress to final output
        import tempfile
        temp_output = None
        if compress:
            temp_output = os.path.join(tempfile.gettempdir(), next(tempfile._get_candidate_names()) + '.tif')
            final_output = output_raster_path
            output_raster_path = temp_output
        else:
            final_output = output_raster_path

        # Get file paths for all input rasters
        input_files = []
        for lyr in raster_layers:
            if isinstance(lyr, QgsRasterLayer):
                input_files.append(lyr.source())
            else:
                feedback.pushWarning(f'Skipping invalid input: {lyr.name()} is not a raster layer.')

        if len(input_files) < 2:
            raise QgsProcessingException('At least two valid raster layers are required to merge.')

        feedback.pushInfo(f'Merging {len(input_files)} rasters...')
        
        # The 'create' script uses -9999.0 as its NoData value. We will use this for consistency.
        nodata_value = -9999.0

        # Parameters for gdal:merge. 
        # We explicitly set the input and output NoData values and initialize the output raster
        # with the NoData value. This prevents areas outside the input extents from being set to 0.
        merge_params = {
            'INPUT': input_files,
            'PCT': False,
            'SEPARATE': False,
            'NODATA_INPUT': nodata_value,
            'NODATA_OUTPUT': nodata_value,  # Correct parameter name for output NoData
            'INIT': nodata_value,           # Initialize output with NoData value
            'DATA_TYPE': 5,                 # 5 = Float32, to preserve floating point depth data
            'OUTPUT': output_raster_path    # Write directly to the final destination
        }
        
        feedback.pushInfo(f'Using GDAL Merge with NoData value: {nodata_value}')
        
        # Run the merge process
        result = processing.run(
            'gdal:merge',
            merge_params,
            context=context,
            feedback=feedback
        )

        if not result or not result.get('OUTPUT'):
            raise QgsProcessingException('Raster merge failed. Check the processing log for more details.')

        # If compression is enabled, use gdal:translate to compress to final output
        if compress:
            feedback.pushInfo('Applying LZW compression using gdal:translate...')
            translate_params = {
                'INPUT': output_raster_path,
                'OUTPUT': final_output,
                'OPTIONS': 'COMPRESS=LZW',
                'DATA_TYPE': 5 # Float32
            }
            result2 = processing.run('gdal:translate', translate_params, context=context, feedback=feedback)
            if not result2 or not result2.get('OUTPUT'):
                raise QgsProcessingException('Compression (gdal:translate) failed. Check the processing log for more details.')
            feedback.pushInfo('Compression complete.')
            return {self.OUTPUT: result2['OUTPUT']}
        else:
            feedback.pushInfo('Merge complete. Original depth values are preserved.')
            feedback.pushInfo(f'Output raster created at: {result["OUTPUT"]}')
            return {self.OUTPUT: result['OUTPUT']}

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
<p>This tool merges multiple MBES raster layers into a single raster, preserving depth (Z) values and ensuring NoData areas are transparent.</p>
<ul>
  <li>Select two or more raster layers created by the MBES XYZ tool.</li>
  <li>The output raster will have the original depth values from the source layers.</li>
  <li>NoData values are correctly handled, resulting in a seamless mosaic with transparent backgrounds where no data exists.</li>
  <li>You can apply a custom color style or ramp to the final merged layer within QGIS for visualisation.</li>
  <li>Useful for mosaicking adjacent or overlapping MBES tiles.</li>
  <li><b>Compression:</b> Enable LZW compression to reduce output file size without losing data.</li>
</ul>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)