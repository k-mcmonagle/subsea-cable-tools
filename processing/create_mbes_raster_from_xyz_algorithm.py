from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterCrs,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFolderDestination,
    QgsProcessingException,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
    QgsFields,
    QgsWkbTypes,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingParameterEnum,
    QgsRectangle,
    QgsProcessingUtils,
    QgsRasterLayer
)
from qgis import processing
import numpy as np
import os

class CreateMBESRasterFromXYZAlgorithm(QgsProcessingAlgorithm):
    """
    Create rasters from XYZ (Easting, Northing, Depth) text files.
    Processes each input file individually to create separate raster layers,
    respecting each file's grid size/resolution.
    Uses a VRT bridge for robust and efficient communication
    with GDAL command-line tools, avoiding common processing errors.
    """
    
    # --- PARAMETER DEFINITIONS ---
    INPUT_XYZ = 'INPUT_XYZ'
    CRS = 'CRS'
    GRID_SIZE = 'GRID_SIZE'
    MAX_DISTANCE = 'MAX_DISTANCE'
    METHOD = 'METHOD'
    OUTPUT = 'OUTPUT'
    COMPRESS = 'COMPRESS'

    def __init__(self):
        super().__init__()

    def initAlgorithm(self, config=None):
        """Initializes the algorithm's parameters."""
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_XYZ,
                self.tr('Input XYZ File(s) (comma-separated for multiple)'),
                behavior=QgsProcessingParameterFile.File,
                fileFilter='XYZ Files (*.xyz *.txt);;All Files (*.*)'
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.CRS,
                self.tr('Coordinate Reference System'),
                defaultValue='EPSG:4326'
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.GRID_SIZE,
                self.tr('Grid Size (0 = auto-detect per file)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_DISTANCE,
                self.tr('Maximum Interpolation Distance (0 = auto)'),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                self.tr('Rasterization Method'),
                options=[
                    self.tr('Direct Rasterization (fast, preserves original data points)'),
                    self.tr('IDW Interpolation (slower, fills gaps)')
                ],
                defaultValue=0,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT,
                self.tr('Output Folder')
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
        """Main processing logic for the algorithm."""
        # --- 1. Get Parameters ---
        # Accept comma-separated list for multiple files, or a single file
        xyz_path_raw = self.parameterAsFile(parameters, self.INPUT_XYZ, context)
        if ',' in xyz_path_raw:
            xyz_paths = [p.strip() for p in xyz_path_raw.split(',') if p.strip()]
        else:
            xyz_paths = [xyz_path_raw]
        target_crs = self.parameterAsCrs(parameters, self.CRS, context)
        grid_size_param = self.parameterAsDouble(parameters, self.GRID_SIZE, context)
        max_distance_param = self.parameterAsDouble(parameters, self.MAX_DISTANCE, context)
        method_index = self.parameterAsInt(parameters, self.METHOD, context)
        output_folder = self.parameterAsString(parameters, self.OUTPUT, context)
        compress = self.parameterAsBool(parameters, self.COMPRESS, context)

        if output_folder == QgsProcessing.TEMPORARY_OUTPUT:
            output_folder = os.path.join(QgsProcessingUtils.tempFolder(), 'xyz_rasters_' + os.urandom(4).hex())
        
        # Ensure output folder exists
        os.makedirs(output_folder, exist_ok=True)

        file_list_str = ', '.join([os.path.basename(p) for p in xyz_paths])
        feedback.pushInfo(f'Starting processing for {len(xyz_paths)} file(s): {file_list_str}')

        # --- 2. Process Each XYZ File Individually ---
        output_paths = []
        for idx, xyz_path in enumerate(xyz_paths):
            if feedback.isCanceled():
                break

            feedback.setProgress(int((idx / len(xyz_paths)) * 100))
            feedback.pushInfo(f'Processing file {idx + 1}/{len(xyz_paths)}: {os.path.basename(xyz_path)}')

            # Detect delimiter
            delimiter = None
            try:
                with open(xyz_path, 'r') as f:
                    line = f.readline()
                    while line.startswith(('#', '//')):
                        line = f.readline()
                    if ',' in line:
                        delimiter = ','
                    else:
                        delimiter = None  # whitespace
            except Exception as e:
                feedback.pushWarning(f'Could not detect delimiter for {os.path.basename(xyz_path)}: {e}. Assuming whitespace.')

            # Read data
            try:
                data = np.loadtxt(xyz_path, comments=['#', '//'], delimiter=delimiter, ndmin=2)
                if data.shape[1] < 3:
                    raise ValueError(f"Input file must have at least 3 columns (X, Y, Z). Found shape: {data.shape}")
                data = data[:, :3]
            except Exception as e:
                raise QgsProcessingException(f'Failed to read or parse XYZ file {os.path.basename(xyz_path)}. Error: {e}')

            if len(data) == 0:
                feedback.pushWarning(f'No data points found in {os.path.basename(xyz_path)}. Skipping.')
                continue

            feedback.pushInfo(f'Successfully read {len(data)} data points from {os.path.basename(xyz_path)}.')

            # --- 2b. Preserve original Z values (depth/elevation) ---
            feedback.pushInfo('Preserving original Z values (depth/elevation) in output raster.')

            # --- 3. Calculate Processing Parameters (per file) ---
            xs, ys = data[:, 0], data[:, 1]
            
            grid_size = grid_size_param
            if grid_size <= 0:
                dx = np.diff(np.sort(np.unique(xs)))
                dy = np.diff(np.sort(np.unique(ys)))
                grid_x = np.median(dx[dx > 1e-9]) if np.any(dx > 1e-9) else 1.0
                grid_y = np.median(dy[dy > 1e-9]) if np.any(dy > 1e-9) else 1.0
                grid_size = float(np.mean([grid_x, grid_y]))
                feedback.pushInfo(f'Auto-detected grid size for this file: {grid_size:.4f}')

            max_distance = max_distance_param
            if method_index == 1 and max_distance <= 0:
                max_distance = grid_size * 3
                feedback.pushInfo(f'Using auto max interpolation distance: {max_distance:.4f}')

            # --- 4. Create VRT Bridge (The Robust Fix) ---
            feedback.pushInfo('Creating temporary CSV and VRT bridge for GDAL...')
            temp_folder = QgsProcessingUtils.tempFolder()
            
            # Create a temporary CSV that GDAL can read
            csv_name = f'points_{idx}.csv'
            temp_csv_path = os.path.join(temp_folder, csv_name)
            np.savetxt(temp_csv_path, data, delimiter=',', header='x,y,z', comments='')
            temp_csv_path = temp_csv_path.replace('\\', '/')
            
            # Source layer name for CSV
            src_layer = os.path.splitext(csv_name)[0]
            
            # Create a VRT file
            vrt_content = f"""<OGRVRTDataSource>
        <OGRVRTLayer name="points">
            <SrcDataSource>{temp_csv_path}</SrcDataSource>
            <SrcLayer>{src_layer}</SrcLayer>
            <GeometryType>wkbPoint25D</GeometryType>
            <LayerSRS>{target_crs.toWkt()}</LayerSRS>
            <GeometryField encoding="PointFromColumns" x="x" y="y" z="z"/>
        </OGRVRTLayer>
    </OGRVRTDataSource>"""
            
            vrt_path = os.path.join(temp_folder, f'points_{idx}.vrt')
            with open(vrt_path, 'w') as f:
                f.write(vrt_content)
            feedback.pushInfo('VRT bridge created successfully.')

            # --- 5. Calculate Raster Extent and Dimensions ---
            extent = QgsRectangle(np.min(xs), np.min(ys), np.max(xs), np.max(ys))
            width = int(np.ceil(extent.width() / grid_size))
            height = int(np.ceil(extent.height() / grid_size))
            
            feedback.pushInfo(f'Output raster dimensions: {width} x {height} pixels')
            feedback.pushInfo(f'Pixel size: {grid_size:.4f}')

            # Determine output path
            base_name = os.path.splitext(os.path.basename(xyz_path))[0]
            final_output = os.path.join(output_folder, f'{base_name}.tif')

            # If compression, create to temp first
            import tempfile
            temp_output = None
            if compress:
                temp_output = os.path.join(temp_folder, next(tempfile._get_candidate_names()) + '.tif')
                output_raster_path = temp_output
            else:
                output_raster_path = final_output

            # --- 6. Execute Chosen Rasterization Method ---
            result = None
            gdal_input = vrt_path
            
            if method_index == 0:  # Direct Rasterization
                feedback.pushInfo('Using direct rasterization method (gdal:rasterize)...')
                rasterize_params = {
                    'INPUT': gdal_input,
                    'FIELD': 'z', # The Z field defined in our VRT
                    'UNITS': 1, # Georeferenced units
                    'WIDTH': grid_size,
                    'HEIGHT': grid_size,
                    'EXTENT': extent,
                    'NODATA': -9999.0,
                    'DATA_TYPE': 5, # Float32
                    'OUTPUT': output_raster_path
                }
                result = processing.run('gdal:rasterize', rasterize_params, context=context, feedback=feedback)

            else:  # IDW Interpolation
                feedback.pushInfo('Using interpolation method (gdal:grididw)...')
                gdal_params = {
                    'INPUT': gdal_input,
                    'Z_FIELD': 'z', # The Z field defined in our VRT
                    'POWER': 2.0,
                    'SMOOTHING': 0.0,
                    'RADIUS': max_distance,
                    'MAX_POINTS': 12,
                    'MIN_POINTS': 1,
                    'NODATA': -9999.0,
                    'DATA_TYPE': 5, # Float32
                    'OUTPUT': output_raster_path,
                    'EXTRA': f'-txe {extent.xMinimum()} {extent.xMaximum()} -tye {extent.yMinimum()} {extent.yMaximum()} -outsize {width} {height}'
                }
                try:
                    result = processing.run('gdal:grididw', gdal_params, context=context, feedback=feedback)
                except QgsProcessingException as e:
                    feedback.pushWarning(f'gdal:grididw failed: {e}. Falling back to native:idwinterpolation.')
                    # For the native QGIS algorithm, we load the VRT as a proper layer first
                    point_layer = QgsVectorLayer(vrt_path, "points_for_idw", "ogr")
                    # Construct INTERPOLATION_DATA string: layer_id::~::field_index::~::use_z::~::type (0 for points)
                    # Since z is field, find index
                    field_index = point_layer.fields().indexFromName('z')
                    interp_data = f"{point_layer.id()}::~::{field_index}::~::0::~::0"
                    idw_params = {
                        'INTERPOLATION_DATA': interp_data,
                        'DISTANCE_COEFFICIENT': 2.0,
                        'EXTENT': extent,
                        'PIXEL_SIZE': grid_size,
                        'OUTPUT': output_raster_path
                    }
                    result = processing.run('native:idwinterpolation', idw_params, context=context, feedback=feedback)

            if not result or not result.get('OUTPUT'):
                raise QgsProcessingException(f'Raster creation failed for {os.path.basename(xyz_path)}. Check the processing log for more details.')

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
                    raise QgsProcessingException(f'Compression (gdal:translate) failed for {os.path.basename(xyz_path)}. Check the processing log for more details.')
                feedback.pushInfo('Compression complete.')
                output_path = result2['OUTPUT']
            else:
                output_path = result['OUTPUT']

            output_paths.append(output_path)

            # Add the raster layer to the project on completion
            details = QgsProcessingContext.LayerDetails(base_name, context.project())
            context.addLayerToLoadOnCompletion(output_path, details)

        feedback.pushInfo('Processing completed successfully.')
        return {self.OUTPUT: output_folder}

    # --- Metadata Functions ---
    def createInstance(self):
        return CreateMBESRasterFromXYZAlgorithm()

    def name(self):
        return 'optimised_creatembesrasterfromxyz'

    def displayName(self):
        return self.tr('Create Raster from XYZ')

    def group(self):
        return self.tr('MBES Tools')

    def groupId(self):
        return 'mbestools'

    def shortHelpString(self):
        return self.tr("""
<h3>Create Raster from XYZ</h3>
<p>This tool converts one or more XYZ files (Easting, Northing, Depth) to raster layers. It processes each file to its own raster if multiple are input, respecting individual grid sizes. It is designed for MBES (Multibeam Echosounder) data but works with any regularly spaced data.</p>

<h4>How it Works</h4>
<ul>
  <li><b>Auto Grid Size:</b> If grid size is 0, the tool will auto-detect an appropriate grid size based on the data in each file.</li>
  <li><b>CRS Selection:</b> Choose the output raster's coordinate reference system (applied to all).</li>
  <li><b>Rasterisation Methods:</b>
    <ul>
      <li><b>Direct Rasterisation:</b> Fast, preserves original data points, but may leave gaps if data is sparse.</li>
      <li><b>IDW Interpolation:</b> Fills gaps using Inverse Distance Weighting, slower but produces a continuous surface.</li>
    </ul>
  </li>
  <li><b>Compression Option:</b> Enable LZW compression to reduce output file size without losing data.</li>
</ul>

<h4>Instructions</h4>
<ol>
  <li><b>Input XYZ File(s):</b> Select a file or paste a comma-separated list of file paths, containing your XYZ data (columns: X, Y, Z).</li>
  <li><b> Set Parameters:</b> Adjust grid size, CRS, method, and compression as needed. Leave grid size at 0 for auto-detect per file.</li>
  <li><b>Output Folder:</b> Specify a folder where the raster files will be saved (named after each input file).</li>
  <li><b>Run:</b> Each output will be created as a separate raster layer with depth or elevation values, with colour grading normalised for each file, and added to the project.</li>
</ol>

<h4>Notes</h4>
<ul>
  <li>Each input file must have at least 3 columns (X, Y, Z).</li>
  <li>Supported delimiters: whitespace or comma.</li>
  <li>For large datasets, direct rasterisation is recommended for speed.</li>
  <li><b>Large Files:</b> XYZ files larger than 100MB may take several minutes to process.</li>
  <li><b>CRS Hint:</b> If you are unsure of the correct projection, check the Survey Report or metadata provided with your data.</li>
  <li>Check the Log Messages Panel for warnings or errors if processing fails.</li>
  <li><b>Multiple Files:</b> You can process multiple XYZ files by entering a comma-separated list of file paths in the input box. The tool will create separate rasters for each, with grid sizes respected per file.</li>
  <li><b>Compression:</b> LZW compression is lossless and can significantly reduce file size for most rasters.</li>
  <li><b>Note:</b> Multiple file selection is not available in all QGIS versions. Use a comma-separated list as a workaround.</li>
</ul>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)