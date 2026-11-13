# -*- coding: utf-8 -*-
"""Create rasters from XYZ (Easting, Northing, Depth) text files.

One raster per input file, so each file keeps its own native resolution;
grid size auto-detects per file (or one explicit override for all). The
point cloud is bridged to GDAL through a temporary CSV + VRT pair, which
avoids the format quirks of feeding XYZ text straight into GDAL tools.
GDAL runs inside QGIS through its Python bindings (:mod:`gdal_inprocess`),
not as separate programs, which some Windows policies refuse to start.
"""

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsRectangle,
)
from . import gdal_inprocess
from .algorithm_base import SubseaCableAlgorithm
from .file_list_parameter import FileListParameter
from ..qgis_compat import (
    PROCESSING_NUMBER_DOUBLE, PROCESSING_NUMBER_INTEGER,
    processing_temp_folder,
)
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

# The Bin Average method holds raster-sized accumulators in RAM (float64
# sums + int64 counts + the Float32 output ≈ 20 bytes per cell, on top of
# the loaded point cloud), so it gets a tighter ceiling: 120M cells ≈ 2.4 GB
# of accumulators. Beyond that, use a larger grid size or Direct/IDW (which
# stream through GDAL).
MAX_AVERAGE_CELLS = 120_000_000


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


def detect_grid_spacing(xs, ys):
    """(x spacing, y spacing): median gap between distinct sorted coordinates.

    Exact for regularly gridded exports (the normal MBES deliverable) even
    with missing cells, because the median of the unique-coordinate gaps is
    the grid step — and kept per-axis, so rectangular-cell exports are not
    misregistered by averaging. Scattered data can under-estimate; the
    raster-size guard catches the pathological results.
    """
    dx = np.diff(np.unique(xs))
    dy = np.diff(np.unique(ys))
    grid_x = float(np.median(dx[dx > 1e-9])) if np.any(dx > 1e-9) else 1.0
    grid_y = float(np.median(dy[dy > 1e-9])) if np.any(dy > 1e-9) else 1.0
    return grid_x, grid_y


def detect_grid_size(xs, ys):
    """Single representative grid size (mean of the per-axis spacings)."""
    grid_x, grid_y = detect_grid_spacing(xs, ys)
    return (grid_x + grid_y) / 2.0


def cell_centred_extent_xy(xs, ys, grid_x, grid_y):
    """Extent padded by half a cell per axis so points sit at cell centres.

    XYZ grid exports give cell-centre coordinates; an extent built from the
    raw min/max would place the outermost points on the raster edge and
    shift every cell half a pixel (the edge row/column can even be dropped).
    """
    return QgsRectangle(
        float(np.min(xs)) - grid_x / 2.0, float(np.min(ys)) - grid_y / 2.0,
        float(np.max(xs)) + grid_x / 2.0, float(np.max(ys)) + grid_y / 2.0,
    )


def cell_centred_extent(xs, ys, grid_size):
    """Square-cell convenience wrapper around :func:`cell_centred_extent_xy`."""
    return cell_centred_extent_xy(xs, ys, grid_size, grid_size)


def bin_average_grid(xs, ys, zs, grid_x, grid_y, x_min, y_max,
                     width, height, nodata=-9999.0):
    """Mean of the points in each pixel — true per-cell binning.

    Row 0 is the top (north) row, matching GeoTIFF layout. Cells with no
    points hold ``nodata``. Unlike a radial gdal_grid average, a point
    contributes to exactly the one cell that contains it.
    """
    col = np.floor((np.asarray(xs, dtype=float) - x_min) / grid_x).astype(np.int64)
    row = np.floor((y_max - np.asarray(ys, dtype=float)) / grid_y).astype(np.int64)
    np.clip(col, 0, width - 1, out=col)
    np.clip(row, 0, height - 1, out=row)
    flat = row * width + col
    counts = np.bincount(flat, minlength=width * height)
    sums = np.bincount(flat, weights=np.asarray(zs, dtype=float),
                       minlength=width * height)
    grid = np.full(width * height, float(nodata), dtype=np.float32)
    filled = counts > 0
    grid[filled] = (sums[filled] / counts[filled]).astype(np.float32)
    return grid.reshape(height, width)


class CreateMBESRasterFromXYZAlgorithm(SubseaCableAlgorithm):
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
            FileListParameter(
                self.INPUT_XYZ,
                self.tr('Input XYZ file(s)'),
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
                self.tr('IDW search radius (0 = auto)'),
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
                processing_temp_folder(context), 'xyz_rasters_' + uuid.uuid4().hex[:8])
        os.makedirs(output_folder, exist_ok=True)

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
                    context, feedback))
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
                     output_folder, compress, context, feedback):
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
        if not np.isfinite(data).all():
            raise QgsProcessingException('XYZ contains nonfinite coordinates/depths; remove or flag invalid soundings before gridding.')
        feedback.pushInfo(f'Read {len(data)} data points.')

        # --- CRS sanity: a geographic CRS with projected-looking coordinates
        # (or vice versa) silently produces a garbage georeference. ---
        xs, ys = data[:, 0], data[:, 1]
        max_x = float(np.max(np.abs(xs)))
        max_y = float(np.max(np.abs(ys)))
        if target_crs.isGeographic() and (max_x > 360.0 or max_y > 90.0):
            raise QgsProcessingException(
                f'{os.path.basename(xyz_path)}: coordinates reach '
                f'({max_x:.1f}, {max_y:.1f}) but the selected CRS '
                f'({target_crs.authid()}) is geographic (degrees). Select the '
                'projected CRS the file was exported in (e.g. the UTM zone).')
        if not target_crs.isGeographic() and max_x <= 180.0 and max_y <= 90.0:
            feedback.pushWarning(
                'Coordinates look like latitude/longitude but the selected '
                f'CRS ({target_crs.authid()}) is projected — check the CRS '
                'if the output lands in the wrong place.')

        # --- per-file grid size (kept per-axis: rectangular cells are not
        # misregistered by averaging the two spacings) ---
        if grid_size_param > 0:
            auto_grid = False
            grid_x = grid_y = grid_size_param
        else:
            auto_grid = True
            grid_x, grid_y = detect_grid_spacing(xs, ys)
            feedback.pushInfo(
                f'Auto-detected grid spacing for this file: {grid_x:.4f} x {grid_y:.4f}')

        def _raster_shape(cell_x, cell_y):
            ext = cell_centred_extent_xy(xs, ys, cell_x, cell_y)
            return (ext, max(1, int(round(ext.width() / cell_x))),
                    max(1, int(round(ext.height() / cell_y))))

        extent, width, height = _raster_shape(grid_x, grid_y)
        if auto_grid and width * height > MAX_RASTER_CELLS:
            # Median-gap detection collapses on scattered (non-gridded)
            # soundings, where nearly every coordinate is unique. Fall back
            # to a density-based estimate (mean point spacing over the
            # bounding box), which is how dedicated importers size the grid.
            area = extent.width() * extent.height()
            if area > 0 and len(data) > 0:
                density_size = float(np.sqrt(area / len(data)))
                if density_size > max(grid_x, grid_y):
                    grid_x = grid_y = density_size
                    extent, width, height = _raster_shape(grid_x, grid_y)
                    feedback.pushWarning(
                        'The data does not look regularly gridded; using a '
                        f'density-based grid size of {density_size:.4f} instead. '
                        'Set an explicit Grid Size to override, and consider '
                        'the Bin Average method for scattered soundings.')
        if width * height > MAX_RASTER_CELLS:
            raise QgsProcessingException(
                f'{os.path.basename(xyz_path)}: a {width} x {height} pixel raster at '
                f'grid spacing {grid_x:.4f} x {grid_y:.4f} is unreasonably large. '
                'The data is probably not regularly gridded, so auto-detection '
                'produced a too-small cell size — set an explicit Grid Size instead.')
        feedback.pushInfo(
            f'Output raster: {width} x {height} pixels at {grid_x:.4f} x {grid_y:.4f}')
        if method_index == self.METHOD_AVERAGE and width * height > MAX_AVERAGE_CELLS:
            est_gb = width * height * 20.0 / 1e9
            raise QgsProcessingException(
                f'{os.path.basename(xyz_path)}: Bin Average would need '
                f'roughly {est_gb:.1f} GB of in-memory accumulators for a '
                f'{width} x {height} raster. Use a larger Grid Size, or the '
                'Direct/IDW method, which stream through GDAL.')

        if method_index == self.METHOD_DIRECT:
            cols = np.floor((data[:,0]-extent.xMinimum())/grid_x).astype(np.int64)
            rows = np.floor((extent.yMaximum()-data[:,1])/grid_y).astype(np.int64)
            duplicate_count = len(data) - len(np.unique(rows * width + cols))
            if duplicate_count:
                feedback.pushWarning(f'{duplicate_count:,} soundings share output cells: Direct keeps the last sounding. Use Bin Average for scattered soundings.')
        grid_mean = (grid_x + grid_y) / 2.0
        max_distance = max_distance_param
        if method_index == self.METHOD_IDW and max_distance <= 0:
            max_distance = grid_mean * 3
            feedback.pushInfo(f'Using auto IDW search radius: {max_distance:.4f}')

        temp_folder = processing_temp_folder(context)
        token = uuid.uuid4().hex[:8]
        temp_csv_path = None
        vrt_path = None
        if method_index != self.METHOD_AVERAGE:
            # --- CSV + VRT bridge for the GDAL-tool methods (unique names:
            # parallel runs must not collide). Bin averaging works on the
            # in-memory array directly and skips this round-trip entirely.
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
                feedback.pushInfo('Using direct rasterization (gdal_rasterize)...')
                result = {'OUTPUT': gdal_inprocess.run('rasterize', output_raster_path, vrt_path, [
                    '-l', 'points', '-a', 'z',
                    '-tr', repr(float(grid_x)), repr(float(grid_y)),   # georeferenced units
                    # Pre-fill the band: without -init, unwritten cells rely
                    # on GDAL initialising to the nodata value.
                    '-init', '-9999.0', '-a_nodata', '-9999.0',
                    '-te', repr(extent.xMinimum()), repr(extent.yMinimum()),
                    repr(extent.xMaximum()), repr(extent.yMaximum()),
                    '-ot', 'Float32', '-of', 'GTiff'], feedback)}
            elif method_index == self.METHOD_AVERAGE:
                result = self._run_average(
                    data, grid_x, grid_y, extent, width, height, target_crs,
                    output_raster_path, feedback)
            else:
                result = self._run_idw(
                    vrt_path, extent, width, height, max_distance,
                    output_raster_path, feedback)

            if not result or not result.get('OUTPUT'):
                raise QgsProcessingException(
                    f'Raster creation failed for {os.path.basename(xyz_path)}. '
                    'Check the processing log for more details.')

            # --- optional gap fill ---
            if fill_cells > 0:
                feedback.pushInfo(
                    f'Filling no-data gaps up to {fill_cells} cell(s) '
                    '(gdal_fillnodata)...')
                fill_path = (os.path.join(temp_folder, f'xyz_{token}_fill.tif')
                             if compress else final_output)
                result = {'OUTPUT': gdal_inprocess.fill_nodata(
                    output_raster_path, fill_path, fill_cells, feedback)}

            # --- compress ---
            if compress:
                feedback.pushInfo('Applying LZW compression (gdal_translate)...')
                output_path = gdal_inprocess.run('translate', final_output, result['OUTPUT'], [
                    '-of', 'GTiff', '-co', 'COMPRESS=LZW', '-co', 'TILED=YES',
                    '-co', 'BIGTIFF=IF_SAFER'], feedback)   # source type kept (Float32)
            else:
                output_path = result['OUTPUT']
        finally:
            # The temp CSV duplicates the whole point cloud; reclaim it per
            # file so a long batch doesn't fill the temp drive.
            stale_paths = [p for p in (temp_csv_path, vrt_path) if p]
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
    def _grid_extent(extent, width, height):
        """The cell-centred extent and exact raster size, so the gdal_grid
        output aligns with the direct-rasterization grid."""
        return ['-txe', repr(extent.xMinimum()), repr(extent.xMaximum()),
                '-tye', repr(extent.yMinimum()), repr(extent.yMaximum()),
                '-outsize', str(int(width)), str(int(height))]

    def _run_idw(self, vrt_path, extent, width, height, max_distance,
                 output_raster_path, feedback):
        # Inverse distance to a power with nearest-neighbour searching
        # (gdal_grid invdistnn, GDAL 2.1+); plain invdist with the same
        # radius in both directions on older GDAL.
        from osgeo import gdal
        radius = repr(float(max_distance))
        if int(gdal.VersionInfo('VERSION_NUM')) >= 2010000:
            feedback.pushInfo('Using IDW interpolation (gdal_grid invdistnn)...')
            algorithm = (f'invdistnn:power=2.0:smoothing=0.0:radius={radius}:max_points=12:'
                         'min_points=1:nodata=-9999.0')
        else:  # pragma: no cover - GDAL 2.0
            feedback.pushInfo('Using IDW interpolation (gdal_grid invdist)...')
            algorithm = (f'invdist:power=2.0:smoothing=0.0:radius1={radius}:radius2={radius}:angle=0.0:'
                         'max_points=12:min_points=1:nodata=-9999.0')
        return {'OUTPUT': gdal_inprocess.run('grid', output_raster_path, vrt_path, [
            '-l', 'points', '-zfield', 'z', '-a', algorithm, '-ot', 'Float32', '-of', 'GTiff',
            *self._grid_extent(extent, width, height)], feedback)}

    def _run_average(self, data, grid_x, grid_y, extent, width, height,
                     target_crs, output_raster_path, feedback):
        """True per-cell binning: mean of the points in each pixel.

        The bin-and-average import dedicated MBES tools use, so dense raw
        soundings grid without last-point-wins noise. Pure NumPy binning
        (each point contributes to exactly its containing cell — no radial
        neighbourhood bleed) written straight through the GDAL bindings, so
        this path needs no temp CSV/VRT round-trip of the point cloud.
        """
        from osgeo import gdal
        feedback.pushInfo('Using bin averaging (per-cell mean, NumPy)...')
        grid = bin_average_grid(
            data[:, 0], data[:, 1], data[:, 2], grid_x, grid_y,
            extent.xMinimum(), extent.yMaximum(), width, height)
        driver = gdal.GetDriverByName('GTiff')
        dataset = driver.Create(output_raster_path, width, height, 1,
                                gdal.GDT_Float32)
        if dataset is None:
            raise QgsProcessingException(
                f'Could not create output raster {output_raster_path}.')
        try:
            dataset.SetGeoTransform((extent.xMinimum(), grid_x, 0.0,
                                     extent.yMaximum(), 0.0, -grid_y))
            dataset.SetProjection(target_crs.toWkt())
            band = dataset.GetRasterBand(1)
            band.SetNoDataValue(-9999.0)
            band.WriteArray(grid)
            band.FlushCache()
        finally:
            dataset = None
        return {'OUTPUT': output_raster_path}

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
  <li><b>Methods:</b> <i>Direct rasterisation</i> burns each point untouched — exact for pre-gridded MBES exports, but where several soundings share a cell only the last one survives. <i>Bin Average</i> writes the mean of the soundings falling in each pixel (true per-cell binning, the behaviour of dedicated MBES importers) — use it for raw/scattered soundings. <i>IDW interpolation</i> distance-weights within a search radius and also fills small gaps, at the cost of smoothing.</li>
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
