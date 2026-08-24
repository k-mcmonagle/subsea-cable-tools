# -*- coding: utf-8 -*-
"""Create rasters from XYZ (Easting, Northing, Depth) text files.

One raster per input file, so each file keeps its own native resolution;
grid size auto-detects per file (or one explicit override for all). The
point cloud is bridged to GDAL through a temporary CSV + VRT pair, which
avoids the format quirks of feeding XYZ text straight into GDAL tools.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsApplication,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingUtils,
    QgsRectangle,
)
from ..qgis_compat import (
    PROCESSING_NUMBER_DOUBLE, PROCESSING_NUMBER_INTEGER,
    PROCESSING_SOURCE_FILE,
)
from qgis import processing
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None
import os
import uuid

# Refuse to build rasters beyond this many cells: a mis-detected grid size on
# scattered (non-gridded) data would otherwise ask GDAL for a raster that
# exhausts memory/disk. ~500M Float32 cells is already a 2 GB uncompressed file.
MAX_RASTER_CELLS = 500_000_000


def sniff_xyz_format(path, probe_lines=10):
    """(delimiter, skiprows) for an XYZ text file.

    Delimiter is ',', ';', or None (whitespace, incl. tabs). Leading comment
    lines (# or //) are handled by the reader; this additionally counts
    leading non-numeric lines (column headers like "Easting Northing Depth")
    so they can be skipped instead of crashing the parse.
    """
    delimiter = None
    skiprows = 0
    with open(path, "r", errors="replace") as handle:
        probed = 0
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "//")):
                continue
            if probed == 0:
                if "," in stripped:
                    delimiter = ","
                elif ";" in stripped:
                    delimiter = ";"
            tokens = stripped.split(delimiter) if delimiter else stripped.split()
            numeric = 0
            for token in tokens:
                try:
                    float(token)
                    numeric += 1
                except ValueError:
                    break
            if numeric >= 3:
                return delimiter, skiprows
            skiprows += 1
            probed += 1
            if probed >= probe_lines:
                break
    return delimiter, skiprows


def detect_grid_size(xs, ys):
    """Median spacing between distinct sorted coordinates, per axis, averaged.

    Exact for regularly gridded exports (the normal MBES deliverable) even
    with missing cells, because the median of the unique-coordinate gaps is
    the grid step. Scattered data can under-estimate; the raster-size guard
    catches the pathological results.
    """
    dx = np.diff(np.unique(xs))
    dy = np.diff(np.unique(ys))
    grid_x = float(np.median(dx[dx > 1e-9])) if np.any(dx > 1e-9) else 1.0
    grid_y = float(np.median(dy[dy > 1e-9])) if np.any(dy > 1e-9) else 1.0
    return (grid_x + grid_y) / 2.0


def cell_centred_extent(xs, ys, grid_size):
    """Extent padded by half a cell so every point sits at a cell centre.

    XYZ grid exports give cell-centre coordinates; an extent built from the
    raw min/max would place the outermost points on the raster edge and
    shift every cell half a pixel (the edge row/column can even be dropped).
    """
    half = grid_size / 2.0
    return QgsRectangle(
        float(np.min(xs)) - half, float(np.min(ys)) - half,
        float(np.max(xs)) + half, float(np.max(ys)) + half,
    )


class CreateMBESRasterFromXYZAlgorithm(QgsProcessingAlgorithm):
    """One raster per XYZ file, native resolution preserved per file."""

    INPUT_XYZ = 'INPUT_XYZ'
    CRS = 'CRS'
    GRID_SIZE = 'GRID_SIZE'
    MAX_DISTANCE = 'MAX_DISTANCE'
    METHOD = 'METHOD'
    FILL_DISTANCE = 'FILL_DISTANCE'
    OUTPUT = 'OUTPUT'
    COMPRESS = 'COMPRESS'

    METHOD_DIRECT = 0
    METHOD_IDW = 1
    METHOD_AVERAGE = 2

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_XYZ,
                self.tr('Input XYZ file(s)'),
                layerType=PROCESSING_SOURCE_FILE,
            )
        )
        self.addParameter(
            QgsProcessingParameterCrs(
                self.CRS,
                self.tr('Coordinate Reference System (all files)'),
                defaultValue='EPSG:4326'
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.GRID_SIZE,
                self.tr('Grid Size (0 = auto-detect per file)'),
                type=PROCESSING_NUMBER_DOUBLE,
                defaultValue=0.0,
                minValue=0.0
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_DISTANCE,
                self.tr('Search radius for IDW / Bin average (0 = auto)'),
                type=PROCESSING_NUMBER_DOUBLE,
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
                    self.tr('Direct Rasterization (gridded exports: burns each point untouched)'),
                    self.tr('IDW Interpolation (scattered soundings: distance-weighted, fills gaps)'),
                    self.tr('Bin Average (scattered soundings: mean of the points in each cell)'),
                ],
                defaultValue=0,
                optional=False
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.FILL_DISTANCE,
                self.tr('Fill no-data gaps up to this many cells (0 = no filling)'),
                type=PROCESSING_NUMBER_INTEGER,
                defaultValue=0,
                minValue=0,
                optional=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT,
                self.tr('Output Folder')
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.COMPRESS,
                self.tr('Apply LZW Compression (smaller file size, no data loss)'),
                defaultValue=True
            )
        )

    # -- input handling --------------------------------------------------------
    def _input_paths(self, parameters, context):
        paths = self.parameterAsFileList(parameters, self.INPUT_XYZ, context) or []
        # Legacy invocations (scripts, saved models) passed one string, possibly
        # a comma-separated list; split only when the joined path is not a file.
        expanded = []
        for path in paths:
            path = str(path).strip()
            if not path:
                continue
            if ',' in path and not os.path.isfile(path):
                expanded.extend(p.strip() for p in path.split(',') if p.strip())
            else:
                expanded.append(path)
        return expanded

    def processAlgorithm(self, parameters, context, feedback):
        if np is None:
            raise QgsProcessingException(
                'NumPy is required for this tool but could not be imported. '
                'Please install/enable NumPy for your QGIS Python environment.'
            )

        def alg_available(algorithm_id: str) -> bool:
            try:
                return QgsApplication.processingRegistry().algorithmById(algorithm_id) is not None
            except Exception:
                return False

        # --- 1. Parameters ---
        xyz_paths = self._input_paths(parameters, context)
        if not xyz_paths:
            raise QgsProcessingException('Select at least one XYZ file.')
        missing = [p for p in xyz_paths if not os.path.isfile(p)]
        if missing:
            raise QgsProcessingException(
                'Input file(s) not found: ' + ', '.join(missing))

        target_crs = self.parameterAsCrs(parameters, self.CRS, context)
        grid_size_param = self.parameterAsDouble(parameters, self.GRID_SIZE, context)
        max_distance_param = self.parameterAsDouble(parameters, self.MAX_DISTANCE, context)
        method_index = self.parameterAsInt(parameters, self.METHOD, context)
        fill_cells = self.parameterAsInt(parameters, self.FILL_DISTANCE, context)
        output_folder = self.parameterAsString(parameters, self.OUTPUT, context)
        compress = self.parameterAsBool(parameters, self.COMPRESS, context)

        if output_folder == QgsProcessing.TEMPORARY_OUTPUT:
            output_folder = os.path.join(
                QgsProcessingUtils.tempFolder(), 'xyz_rasters_' + uuid.uuid4().hex[:8])
        os.makedirs(output_folder, exist_ok=True)

        required = {
            self.METHOD_DIRECT: ('gdal:rasterize',),
            self.METHOD_IDW: ('gdal:gridinversedistancenearestneighbor',
                              'gdal:gridinversedistance'),
            self.METHOD_AVERAGE: ('gdal:gridaverage',),
        }.get(method_index, ())
        if required and not any(alg_available(a) for a in required):
            raise QgsProcessingException(
                'Required Processing algorithm %s is not available. This '
                'usually means the GDAL Processing provider is not installed '
                'or not enabled.' % ' / '.join(required))
        if fill_cells > 0 and not alg_available('gdal:fillnodata'):
            raise QgsProcessingException(
                "Gap filling requested but 'gdal:fillnodata' is not available. "
                'Enable the GDAL Processing provider or set gap filling to 0.')

        file_list_str = ', '.join(os.path.basename(p) for p in xyz_paths)
        feedback.pushInfo(f'Starting processing for {len(xyz_paths)} file(s): {file_list_str}')

        # --- 2. Each file individually (its own grid size, its own raster) ---
        output_paths = []
        failures = []
        for idx, xyz_path in enumerate(xyz_paths):
            if feedback.isCanceled():
                break
            feedback.setProgress(int((idx / len(xyz_paths)) * 100))
            feedback.pushInfo(
                f'--- File {idx + 1}/{len(xyz_paths)}: {os.path.basename(xyz_path)} ---')
            try:
                output_paths.append(self._process_one(
                    xyz_path, target_crs, grid_size_param, max_distance_param,
                    method_index, fill_cells, output_folder, compress,
                    context, feedback, alg_available))
            except QgsProcessingException as exc:
                if len(xyz_paths) == 1:
                    raise
                failures.append(os.path.basename(xyz_path))
                feedback.pushWarning(
                    f'{os.path.basename(xyz_path)} failed and was skipped: {exc}')

        feedback.setProgress(100)
        if failures:
            feedback.pushWarning(
                f'{len(failures)} of {len(xyz_paths)} file(s) failed: '
                + ', '.join(failures))
        if not output_paths:
            raise QgsProcessingException(
                'No rasters were created. Check the log for per-file errors.')
        feedback.pushInfo(f'Created {len(output_paths)} raster(s) in {output_folder}.')
        return {self.OUTPUT: output_folder}

    def _process_one(self, xyz_path, target_crs, grid_size_param,
                     max_distance_param, method_index, fill_cells,
                     output_folder, compress, context, feedback,
                     alg_available):
        base_name = os.path.splitext(os.path.basename(xyz_path))[0]

        # --- read ---
        try:
            delimiter, skiprows = sniff_xyz_format(xyz_path)
        except Exception as exc:
            raise QgsProcessingException(
                f'Could not inspect {os.path.basename(xyz_path)}: {exc}')
        if skiprows:
            feedback.pushInfo(f'Skipping {skiprows} header line(s).')
        try:
            data = np.loadtxt(xyz_path, comments=['#', '//'],
                              delimiter=delimiter, skiprows=skiprows, ndmin=2)
            if data.shape[1] < 3:
                raise ValueError(
                    f'Input file must have at least 3 columns (X, Y, Z). Found shape: {data.shape}')
            data = data[:, :3]
        except Exception as exc:
            raise QgsProcessingException(
                f'Failed to read or parse XYZ file {os.path.basename(xyz_path)}. Error: {exc}')
        if len(data) == 0:
            raise QgsProcessingException(
                f'No data points found in {os.path.basename(xyz_path)}.')
        feedback.pushInfo(f'Read {len(data)} data points.')

        # --- per-file grid size ---
        xs, ys = data[:, 0], data[:, 1]
        grid_size = grid_size_param
        auto_grid = grid_size <= 0
        if auto_grid:
            grid_size = detect_grid_size(xs, ys)
            feedback.pushInfo(f'Auto-detected grid size for this file: {grid_size:.4f}')

        def _raster_shape(cell):
            ext = cell_centred_extent(xs, ys, cell)
            return (ext, max(1, int(round(ext.width() / cell))),
                    max(1, int(round(ext.height() / cell))))

        extent, width, height = _raster_shape(grid_size)
        if auto_grid and width * height > MAX_RASTER_CELLS:
            # Median-gap detection collapses on scattered (non-gridded)
            # soundings, where nearly every coordinate is unique. Fall back
            # to a density-based estimate (mean point spacing over the
            # bounding box), which is how dedicated importers size the grid.
            area = extent.width() * extent.height()
            if area > 0 and len(data) > 0:
                density_size = float(np.sqrt(area / len(data)))
                if density_size > grid_size:
                    grid_size = density_size
                    extent, width, height = _raster_shape(grid_size)
                    feedback.pushWarning(
                        'The data does not look regularly gridded; using a '
                        f'density-based grid size of {grid_size:.4f} instead. '
                        'Set an explicit Grid Size to override, and consider '
                        'the Bin Average method for scattered soundings.')
        if width * height > MAX_RASTER_CELLS:
            raise QgsProcessingException(
                f'{os.path.basename(xyz_path)}: a {width} x {height} pixel raster at '
                f'grid size {grid_size:.4f} is unreasonably large. The data is '
                'probably not regularly gridded, so auto-detection produced a '
                'too-small cell size — set an explicit Grid Size instead.')
        feedback.pushInfo(f'Output raster: {width} x {height} pixels at {grid_size:.4f}')

        max_distance = max_distance_param
        if method_index == self.METHOD_IDW and max_distance <= 0:
            max_distance = grid_size * 3
            feedback.pushInfo(f'Using auto IDW search radius: {max_distance:.4f}')
        elif method_index == self.METHOD_AVERAGE and max_distance <= 0:
            # Circumscribed cell radius: every point in the cell contributes
            # to its own cell with minimal bleed into the neighbours — the
            # bin-and-average behaviour of dedicated MBES importers.
            max_distance = grid_size * 0.7071
            feedback.pushInfo(f'Using auto bin-average radius: {max_distance:.4f}')

        # --- CSV + VRT bridge (unique names: parallel runs must not collide) ---
        temp_folder = QgsProcessingUtils.tempFolder()
        token = uuid.uuid4().hex[:8]
        csv_name = f'xyz_{token}.csv'
        temp_csv_path = os.path.join(temp_folder, csv_name)
        np.savetxt(temp_csv_path, data, delimiter=',', header='x,y,z',
                   comments='', fmt='%.12f')
        vrt_path = os.path.join(temp_folder, f'xyz_{token}.vrt')
        with open(vrt_path, 'w') as handle:
            handle.write(f"""<OGRVRTDataSource>
    <OGRVRTLayer name="points">
        <SrcDataSource>{temp_csv_path.replace(os.sep, '/')}</SrcDataSource>
        <SrcLayer>{os.path.splitext(csv_name)[0]}</SrcLayer>
        <GeometryType>wkbPoint25D</GeometryType>
        <LayerSRS>{target_crs.toWkt()}</LayerSRS>
        <GeometryField encoding="PointFromColumns" x="x" y="y" z="z"/>
    </OGRVRTLayer>
</OGRVRTDataSource>""")

        final_output = os.path.join(output_folder, f'{base_name}.tif')
        fill_path = None
        if compress or fill_cells > 0:
            output_raster_path = os.path.join(temp_folder, f'xyz_{token}_raw.tif')
        else:
            output_raster_path = final_output

        try:
            # --- rasterize ---
            if method_index == self.METHOD_DIRECT:
                feedback.pushInfo('Using direct rasterization (gdal:rasterize)...')
                result = processing.run('gdal:rasterize', {
                    'INPUT': vrt_path,
                    'FIELD': 'z',
                    'UNITS': 1,               # georeferenced units
                    'WIDTH': grid_size,
                    'HEIGHT': grid_size,
                    'EXTENT': extent,
                    'NODATA': -9999.0,
                    # Pre-fill the band: without -init, unwritten cells rely
                    # on GDAL initialising to the nodata value.
                    'INIT': -9999.0,
                    'DATA_TYPE': 5,           # Float32
                    'OUTPUT': output_raster_path,
                }, context=context, feedback=feedback)
            elif method_index == self.METHOD_AVERAGE:
                result = self._run_average(
                    vrt_path, extent, width, height, max_distance,
                    output_raster_path, context, feedback)
            else:
                result = self._run_idw(
                    vrt_path, extent, width, height, max_distance,
                    output_raster_path, context, feedback, alg_available)

            if not result or not result.get('OUTPUT'):
                raise QgsProcessingException(
                    f'Raster creation failed for {os.path.basename(xyz_path)}. '
                    'Check the processing log for more details.')

            # --- optional gap fill ---
            if fill_cells > 0:
                feedback.pushInfo(
                    f'Filling no-data gaps up to {fill_cells} cell(s) '
                    '(gdal:fillnodata)...')
                fill_path = (os.path.join(temp_folder, f'xyz_{token}_fill.tif')
                             if compress else final_output)
                fill_result = processing.run('gdal:fillnodata', {
                    'INPUT': output_raster_path,
                    'BAND': 1,
                    'DISTANCE': fill_cells,
                    'ITERATIONS': 0,
                    'NO_MASK': False,
                    'OUTPUT': fill_path,
                }, context=context, feedback=feedback)
                if not fill_result or not fill_result.get('OUTPUT'):
                    raise QgsProcessingException(
                        f'Gap filling (gdal:fillnodata) failed for {os.path.basename(xyz_path)}.')
                result = fill_result

            # --- compress ---
            if compress:
                feedback.pushInfo('Applying LZW compression (gdal:translate)...')
                if not alg_available('gdal:translate'):
                    raise QgsProcessingException(
                        "Compression requested but 'gdal:translate' is not available. "
                        'Enable the GDAL Processing provider or disable compression.')
                result2 = processing.run('gdal:translate', {
                    'INPUT': result['OUTPUT'],
                    'OUTPUT': final_output,
                    'OPTIONS': 'COMPRESS=LZW|TILED=YES|BIGTIFF=IF_SAFER',
                    'DATA_TYPE': 0,           # keep source type (Float32)
                }, context=context, feedback=feedback)
                if not result2 or not result2.get('OUTPUT'):
                    raise QgsProcessingException(
                        f'Compression (gdal:translate) failed for {os.path.basename(xyz_path)}.')
                output_path = result2['OUTPUT']
            else:
                output_path = result['OUTPUT']
        finally:
            # The temp CSV duplicates the whole point cloud; reclaim it per
            # file so a long batch doesn't fill the temp drive.
            stale_paths = [temp_csv_path, vrt_path]
            if output_raster_path != final_output:
                stale_paths.append(output_raster_path)
            if fill_path and fill_path != final_output:
                stale_paths.append(fill_path)
            for stale in stale_paths:
                try:
                    os.remove(stale)
                except OSError:
                    pass

        details = QgsProcessingContext.LayerDetails(base_name, context.project())
        context.addLayerToLoadOnCompletion(output_path, details)
        return output_path

    @staticmethod
    def _grid_extra(extent, width, height):
        """gdal_grid has no EXTENT/SIZE parameters in Processing; pass the
        cell-centred extent and exact raster size through EXTRA so the output
        aligns with the direct-rasterization grid."""
        return (f'-txe {extent.xMinimum()} {extent.xMaximum()} '
                f'-tye {extent.yMinimum()} {extent.yMaximum()} '
                f'-outsize {width} {height}')

    def _run_idw(self, vrt_path, extent, width, height, max_distance,
                 output_raster_path, context, feedback, alg_available):
        # 'gdal:grididw' (used by earlier plugin versions) never existed in
        # QGIS Processing, so IDW silently fell back to a broken
        # native:idwinterpolation call. These are the real algorithm ids.
        if alg_available('gdal:gridinversedistancenearestneighbor'):
            feedback.pushInfo(
                'Using IDW interpolation '
                '(gdal:gridinversedistancenearestneighbor)...')
            return processing.run('gdal:gridinversedistancenearestneighbor', {
                'INPUT': vrt_path,
                'Z_FIELD': 'z',
                'POWER': 2.0,
                'SMOOTHING': 0.0,
                'RADIUS': max_distance,
                'MAX_POINTS': 12,
                'MIN_POINTS': 1,
                'NODATA': -9999.0,
                'DATA_TYPE': 5,           # Float32
                'EXTRA': self._grid_extra(extent, width, height),
                'OUTPUT': output_raster_path,
            }, context=context, feedback=feedback)
        feedback.pushInfo('Using IDW interpolation (gdal:gridinversedistance)...')
        return processing.run('gdal:gridinversedistance', {
            'INPUT': vrt_path,
            'Z_FIELD': 'z',
            'POWER': 2.0,
            'SMOOTHING': 0.0,
            'RADIUS_1': max_distance,
            'RADIUS_2': max_distance,
            'MAX_POINTS': 12,
            'MIN_POINTS': 1,
            'NODATA': -9999.0,
            'DATA_TYPE': 5,               # Float32
            'EXTRA': self._grid_extra(extent, width, height),
            'OUTPUT': output_raster_path,
        }, context=context, feedback=feedback)

    def _run_average(self, vrt_path, extent, width, height, radius,
                     output_raster_path, context, feedback):
        """Mean of the soundings around each cell centre (gdal:gridaverage) —
        the bin-and-average import dedicated MBES tools use, so dense raw
        soundings grid without last-point-wins noise."""
        feedback.pushInfo('Using bin averaging (gdal:gridaverage)...')
        return processing.run('gdal:gridaverage', {
            'INPUT': vrt_path,
            'Z_FIELD': 'z',
            'RADIUS_1': radius,
            'RADIUS_2': radius,
            'ANGLE': 0.0,
            'MIN_POINTS': 1,
            'NODATA': -9999.0,
            'DATA_TYPE': 5,               # Float32
            'EXTRA': self._grid_extra(extent, width, height),
            'OUTPUT': output_raster_path,
        }, context=context, feedback=feedback)

    # --- Metadata ---
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
<p>Converts one or more XYZ files (Easting, Northing, Depth) to raster layers — one raster per file, so each file keeps its own native resolution. Designed for MBES (multibeam echosounder) grid exports but works with any regularly spaced data.</p>

<h4>How it works</h4>
<ul>
  <li><b>Multiple files:</b> select any number of XYZ files in one run; each becomes its own GeoTIFF named after the file.</li>
  <li><b>Auto grid size:</b> with Grid Size at 0, the cell size is detected per file from the data spacing, so mixed-resolution deliveries (e.g. 0.5&nbsp;m and 1&nbsp;m tiles) each keep full accuracy.</li>
  <li><b>Cell registration:</b> XYZ coordinates are treated as cell centres, so output pixels align exactly with the source grid (no half-pixel shift).</li>
  <li><b>One CRS:</b> the chosen CRS applies to every file in the run.</li>
  <li><b>Methods:</b> <i>Direct rasterisation</i> burns each point untouched — exact for pre-gridded MBES exports, but where several soundings share a cell only the last one survives. <i>Bin Average</i> writes the mean of the soundings around each cell centre (the behaviour of dedicated MBES importers) — use it for raw/scattered soundings. <i>IDW interpolation</i> distance-weights within a search radius and also fills small gaps, at the cost of smoothing.</li>
  <li><b>Gap filling:</b> optionally interpolate no-data holes up to N cells across (GDAL fillnodata) after gridding; 0 leaves gaps as no-data.</li>
  <li><b>Compression:</b> LZW is lossless; outputs are tiled for fast display.</li>
</ul>

<h4>Notes</h4>
<ul>
  <li>Files need at least 3 numeric columns (X, Y, Z); extra columns are ignored. Comma, semicolon, tab or space delimited; leading header lines are skipped automatically.</li>
  <li>Auto grid size uses the median data spacing for gridded exports; on scattered soundings it falls back to a density-based estimate with a warning — set an explicit Grid Size to control it.</li>
  <li>In a multi-file run a bad file is skipped with a warning; the rest still process.</li>
  <li>To mosaic the results, use <i>Merge MBES Rasters</i> — it keeps the finest input resolution.</li>
</ul>
""")

    def tr(self, string):
        return QCoreApplication.translate('Processing', string)
