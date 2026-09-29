"""Shared QGIS bathymetry sampling and explicit per-layer conventions.

Values returned to profile consumers are metres, positive down. Bilinear
sampling uses native cell centres and requires every contributing cell.
"""
import json
import logging
import math
import os
from collections import OrderedDict

from qgis.core import QgsPointXY, QgsRasterLayer, QgsProject, QgsRectangle, QgsUnitTypes
from qgis.PyQt import sip
from .kp_range_utils import make_distance_area
from .plugin_log import log_debug, log_exception, log_info
from .slope_utils import is_finite
from .qgis_compat import DISTANCE_METERS

try:  # numpy ships with QGIS; without it the sampler reads cell by cell.
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

PREFIX = 'subsea/bathymetry/'
VERSION = 'supported-bilinear-v3-terrace-baseline'

# Cells per tile side for block reads, and the tile-cache budget per sampler.
# A 128 x 128 float64 tile is 128 KiB, so 32 MiB keeps 256 tiles — far more
# than the working set of a route profile with cross-slope offsets.
TILE_CELLS = 128
TILE_CACHE_BYTES = 32 * 1024 * 1024
# Scalar sampling checks tile reuse every _THRASH_WINDOW tile reads: fewer
# than _MIN_CELLS_PER_READ cells served per read means scattered queries over
# a raster larger than the cache, where one GDAL-cached sample() per cell is
# cheaper than re-reading whole tiles, so scalar reads go back to per-cell.
_THRASH_WINDOW = 16
_MIN_CELLS_PER_READ = 32
# qgsDoubleNear()'s default tolerance, used by QGIS's own no-data tests.
_NEAR = 4 * 2.220446049250313e-16


def _numpy_dtypes():
    """``Qgis.DataType`` -> numpy dtype for the numeric block types."""
    from qgis.core import Qgis
    scope = getattr(Qgis, 'DataType', Qgis)
    names = {'Byte': 'uint8', 'Int8': 'int8', 'UInt16': 'uint16', 'Int16': 'int16',
             'UInt32': 'uint32', 'Int32': 'int32', 'Float32': 'float32',
             'Float64': 'float64'}
    table = {}
    for name, dtype in names.items():
        value = getattr(scope, name, None)
        if value is not None:
            table[value] = np.dtype(dtype)
    return table


_DTYPES = None


def layer_options(layer):
    return {key: layer.customProperty(PREFIX + key, default) for key, default in
            [('vertical', 'auto'), ('units', 'm'), ('datum', ''),
             ('sampling', 'bilinear'), ('native_cell_m', 0.0)]}


def normalise_depth(value, options):
    if not is_finite(value):
        return None
    v = float(value) * {'m': 1.0, 'ft': 0.3048}.get(options.get('units'), 1.0)
    convention = options.get('vertical', 'auto')
    if convention == 'auto':
        # Infer once per source, never fold every individual value with abs().
        convention = options.setdefault('_inferred_vertical', 'elevation' if v < 0 else 'depth')
    return -v if convention == 'elevation' else v


def native_cell_m(layer, transform_context=None):
    """Native cell size in metres (the larger axis, or the user override).

    Pass ``transform_context`` off the main thread (e.g. the Processing
    context's): the default reads the current project, which is not
    thread-safe.
    """
    override = float(layer_options(layer).get('native_cell_m') or 0)
    c = layer.extent().center()
    dx = abs(layer.rasterUnitsPerPixelX())
    dy = abs(layer.rasterUnitsPerPixelY())
    if layer.crs().isGeographic():
        if transform_context is None:
            transform_context = QgsProject.instance().transformContext()
        da = make_distance_area(layer.crs(), transform_context)
        x = da.measureLine(c, QgsPointXY(c.x() + dx, c.y()))
        y = da.measureLine(c, QgsPointXY(c.x(), c.y() + dy))
    else:
        factor = metres_per_unit(layer.crs())
        x, y = dx * factor, dy * factor
    return max(float(x), float(y), override)


def metres_per_unit(crs):
    return QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), DISTANCE_METERS)


def expand_rasters(layers, seen=None, transform_context=None):
    """New mosaics carry a sidecar; engineering reads their native sources.

    A missing native file fails explicitly rather than trusting upsampled
    output pixels as native resolution. Ordinary/legacy rasters are retained.
    ``transform_context``: see :func:`native_cell_m` (used for the sort).
    """
    seen = set() if seen is None else seen
    out = []
    for layer in layers:
        path = layer.source().split('|')[0]
        manifest = path + '.sources.json'
        if not os.path.isfile(manifest):
            out.append(layer)
            continue
        canonical = os.path.realpath(path)
        if canonical in seen:
            raise ValueError('Circular MBES source manifest: ' + path)
        with open(manifest, encoding='utf-8') as stream:
            records = json.load(stream)['sources']
        children = []
        overrides = {key[len(PREFIX):]: layer.customProperty(key)
                     for key in layer.customPropertyKeys() if key.startswith(PREFIX)}
        for record in records:
            child = QgsRasterLayer(record['path'], record.get('name', os.path.basename(record['path'])))
            if not child.isValid():
                raise ValueError('Native MBES source unavailable: ' + record['path'])
            for key, value in {**record.get('options', {}), **overrides}.items():
                child.setCustomProperty(PREFIX + key, value)
            children.append(child)
        out.extend(expand_rasters(children, seen | {canonical}, transform_context))
    # Stable finest-first across all consumers. Canonical URI is provenance.
    return sorted(out, key=lambda layer: native_cell_m(layer, transform_context))


class RasterSampler:
    """Native-cell sampler for one raster band (metres, positive down).

    Cells are read in ``tile_cells`` x ``tile_cells`` tiles, one
    ``provider.block()`` call each, and kept in an LRU tile cache bounded by
    ``cache_bytes``. A tile applies the same missing-cell rules as the
    per-cell ``provider.sample()`` reads it replaces (non-finite values,
    source no-data, user no-data ranges), and three of its cells are
    cross-checked against ``provider.sample()``: a provider whose blocks do
    not reproduce the native grid switches the sampler back to per-cell
    reads rather than returning shifted values. ``tile_cells=0`` (or no
    numpy) keeps the per-cell path. Scalar ``sample()`` calls also return to
    per-cell reads when tiles are not being reused (scattered queries over a
    raster larger than the cache); ``sample_many`` groups its cells by tile
    and always benefits.

    ``clone=True`` samples through a private provider clone (for worker
    threads). The sampler owns that clone: it and its GDAL file handle are
    freed when the sampler is dropped or ``close()``d. Pass
    ``transform_context`` when constructing off the main thread (see
    :func:`native_cell_m`).
    """

    def __init__(self, layer, band=1, clone=False, tile_cells=TILE_CELLS,
                 cache_bytes=TILE_CACHE_BYTES, transform_context=None):
        self.layer = layer  # retain lifetime, including expanded native layers
        if clone:
            # PyQGIS hands the clone over as C++-owned, so it (and its open
            # file, locked on Windows until QGIS exits) was never freed.
            self.provider = layer.dataProvider().clone()
            sip.transferback(self.provider)
        else:
            self.provider = layer.dataProvider()
        self._owns_provider = bool(clone)
        self.options = layer_options(layer)
        self.band = band
        self.source_id = layer.source()
        self.cell_m = native_cell_m(layer, transform_context)
        self.extent = self.provider.extent()
        self.nx, self.ny = self.provider.xSize(), self.provider.ySize()
        self.dx = self.extent.width() / self.nx if self.nx else 0
        self.dy = self.extent.height() / self.ny if self.ny else 0
        self.cache = OrderedDict()
        self.tile_cells = max(0, int(tile_cells or 0)) if np is not None else 0
        if self.tile_cells and (self.provider.bandScale(band) != 1.0
                                or self.provider.bandOffset(band) != 0.0):
            # QGIS converts a scaled block to Float32, while sample() applies
            # scale/offset in double precision: tiles would round values
            # differently, so scaled bands keep the exact per-cell reads.
            self.tile_cells = 0
        self.cache_bytes = max(0, int(cache_bytes))
        self._tiles = OrderedDict()
        self._tile_bytes = 0
        self._last_key = self._last_tile = None
        self._nodata_rules = None
        # Tile reuse accounting for scalar reads (see _THRASH_WINDOW).
        self._scalar_tiles = True
        self._window_reads = self._window_hits = 0

    def clear_cache(self):
        """Drop cached cells and tiles (e.g. after the source changed)."""
        self.cache.clear()
        self._tiles.clear()
        self._tile_bytes = 0
        self._last_key = self._last_tile = None

    def close(self):
        """Free the caches and, for ``clone=True``, the provider clone and
        its file handle now (other holders of ``provider`` see a deleted
        object). The sampler cannot sample afterwards. Safe to call twice."""
        self.clear_cache()
        provider, self.provider = self.provider, None
        if self._owns_provider and provider is not None and not sip.isdeleted(provider):
            sip.delete(provider)
        self.layer = None

    def _require_open(self):
        if self.provider is None:
            raise RuntimeError('RasterSampler for %s is closed' % self.source_id)

    # -- cell access ---------------------------------------------------------
    def _cell(self, col, row):
        if col < 0 or row < 0 or col >= self.nx or row >= self.ny:
            return None
        size = self.tile_cells
        if size and self._scalar_tiles:
            tile = self._tile(col // size, row // size)
            if tile is not None:
                self._window_hits += 1
                value = tile[row % size, col % size]
                return None if value != value else float(value)
        return self._sampled_cell(col, row)

    def _sampled_cell(self, col, row):
        """One cell through ``provider.sample()`` (the reference semantics)."""
        key = (col, row)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        point = QgsPointXY(self.extent.xMinimum() + (col + .5) * self.dx,
                           self.extent.yMaximum() - (row + .5) * self.dy)
        value, ok = self.provider.sample(point, self.band)
        if not ok or not is_finite(value):
            value = None
        elif self.provider.sourceHasNoDataValue(self.band) and value == self.provider.sourceNoDataValue(self.band):
            value = None
        else:
            value = float(value)
        self.cache[key] = value
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return value

    def _cells(self, cols, rows):
        """Values of many cells (numpy int arrays) as float64, NaN = missing."""
        out = np.full(cols.shape, np.nan)
        inside = np.nonzero((cols >= 0) & (rows >= 0) & (cols < self.nx) & (rows < self.ny))[0]
        if not inside.size:
            return out
        size = self.tile_cells
        if not size:  # tiles were disabled part-way through a batch
            out[inside] = [np.nan if v is None else v for v in
                           (self._sampled_cell(int(c), int(r))
                            for c, r in zip(cols[inside], rows[inside]))]
            return out
        tile_cols, tile_rows = cols[inside] // size, rows[inside] // size
        keys = tile_rows * (self.nx // size + 1) + tile_cols
        for key in np.unique(keys):
            chosen = keys == key
            sel = inside[chosen]
            tx, ty = int(tile_cols[chosen][0]), int(tile_rows[chosen][0])
            tile = self._tile(tx, ty) if self.tile_cells else None
            if tile is None:
                out[sel] = [np.nan if v is None else v for v in
                            (self._sampled_cell(int(c), int(r))
                             for c, r in zip(cols[sel], rows[sel]))]
            else:
                self._window_hits += len(sel)
                out[sel] = tile[rows[sel] - ty * size, cols[sel] - tx * size]
        return out

    # -- tiles ---------------------------------------------------------------
    def _tile(self, tx, ty):
        key = (tx, ty)
        if key == self._last_key:  # consecutive cells mostly share a tile
            return self._last_tile
        tile = self._tiles.get(key)
        if tile is not None:
            self._tiles.move_to_end(key)
        else:
            tile = self._read_tile(tx, ty)
            if tile is None:
                return None
            self._count_read()
            self._tiles[key] = tile
            self._tile_bytes += tile.nbytes
            while self._tile_bytes > self.cache_bytes and len(self._tiles) > 1:
                _key, old = self._tiles.popitem(last=False)
                self._tile_bytes -= old.nbytes
        self._last_key, self._last_tile = key, tile
        return tile

    def _count_read(self):
        """Send scalar reads back to per-cell when tiles are being thrashed."""
        self._window_reads += 1
        if self._window_reads < _THRASH_WINDOW:
            return
        if self._scalar_tiles and self._window_hits < _MIN_CELLS_PER_READ * self._window_reads:
            self._scalar_tiles = False
            log_debug('Bathymetry sampling: %d cells per tile read for %s; scalar '
                      'samples use per-cell reads.' % (
                          self._window_hits // self._window_reads, self.source_id))
        self._window_reads = self._window_hits = 0

    def _read_tile(self, tx, ty):
        size = self.tile_cells
        c0, r0 = tx * size, ty * size
        c1, r1 = min(c0 + size, self.nx), min(r0 + size, self.ny)
        x0, y1 = self.extent.xMinimum(), self.extent.yMaximum()
        rect = QgsRectangle(x0 + c0 * self.dx, y1 - r1 * self.dy,
                            x0 + c1 * self.dx, y1 - r0 * self.dy)
        try:
            values = self._block_values(rect, c1 - c0, r1 - r0)
        except Exception:  # noqa: BLE001 - any provider failure: per-cell reads
            log_exception('Bathymetry sampling: block read failed for %s' % self.source_id,
                          level=logging.DEBUG)
            values = None
        if values is None:
            self._disable_tiles('the provider returned no usable block')
            return None
        if not self._tile_agrees(values, c0, r0):
            self._disable_tiles('block values differ from per-cell sampling')
            return None
        return values

    def _block_values(self, rect, width, height):
        global _DTYPES
        block = self.provider.block(self.band, rect, width, height)
        if (block is None or not block.isValid() or block.width() != width
                or block.height() != height):
            return None
        if _DTYPES is None:
            _DTYPES = _numpy_dtypes()
        dtype = _DTYPES.get(block.dataType())
        if dtype is None:  # complex / colour blocks: not bathymetry
            return None
        data = bytes(block.data())
        if len(data) != width * height * dtype.itemsize:
            return None
        values = np.frombuffer(data, dtype=dtype).reshape(height, width).astype(np.float64)
        values[self._missing(values)] = np.nan
        return values

    def _missing(self, values):
        """Cells ``provider.sample()`` reports as no-data, plus the checks
        the per-cell path adds (non-finite, exact source no-data)."""
        if self._nodata_rules is None:
            provider, band = self.provider, self.band
            source = (float(provider.sourceNoDataValue(band))
                      if provider.sourceHasNoDataValue(band) else None)
            use_source = source is not None and bool(provider.useSourceNoDataValue(band))
            ranges = [(float(r.min()), float(r.max()), r.bounds())
                      for r in provider.userNoDataValues(band)]
            self._nodata_rules = (source, use_source, ranges)
        source, use_source, ranges = self._nodata_rules
        missing = ~np.isfinite(values)
        if source is not None:
            missing |= values == source
            if use_source:
                missing |= np.abs(values - source) <= _NEAR
        if ranges:
            from qgis.core import QgsRasterRange
            include_min = (QgsRasterRange.IncludeMinAndMax, QgsRasterRange.IncludeMin)
            include_max = (QgsRasterRange.IncludeMinAndMax, QgsRasterRange.IncludeMax)
            for low, high, bounds in ranges:
                # QgsRasterRange.contains(), vectorised.
                above = True if math.isnan(low) else (
                    (values > low) | ((np.abs(values - low) <= _NEAR) & (bounds in include_min)))
                below = True if math.isnan(high) else (
                    (values < high) | ((np.abs(values - high) <= _NEAR) & (bounds in include_max)))
                missing |= above & below
        return missing

    def _tile_agrees(self, values, c0, r0):
        height, width = values.shape
        for r, c in {(0, 0), (height // 2, width // 2), (height - 1, width - 1)}:
            expected = self._sampled_cell(c0 + c, r0 + r)
            got = values[r, c]
            if expected is None:
                if got == got:
                    return False
            elif not got == expected:
                return False
        return True

    def _disable_tiles(self, reason):
        if self.tile_cells:
            log_info('Bathymetry sampling: %s for %s; reading it cell by cell '
                     '(same values, slower).' % (reason, self.source_id))
        self.tile_cells = 0
        self._tiles.clear()
        self._tile_bytes = 0
        self._last_key = self._last_tile = None

    # -- sampling ------------------------------------------------------------
    def sample(self, point, method=None):
        self._require_open()
        if not self.extent.contains(point) or self.dx <= 0 or self.dy <= 0:
            return None
        x = (point.x() - self.extent.xMinimum()) / self.dx - .5
        y = (self.extent.yMaximum() - point.y()) / self.dy - .5
        if (method or self.options.get('sampling')) == 'nearest':
            value = self._cell(int(math.floor(x + .5)), int(math.floor(y + .5)))
        else:
            c, r = math.floor(x), math.floor(y)
            fx, fy = x - c, y - r
            value = 0.0
            for dc, dr, w in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                              (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
                if w <= 1e-12:
                    continue
                z = self._cell(c + dc, r + dr)
                if z is None:
                    return None
                value += w * z
        return normalise_depth(value, self.options)

    def sample_many(self, points, method=None):
        """``[self.sample(p, method) for p in points]``, vectorised.

        Bit-identical to the scalar loop — same cell arithmetic, same
        summation order — and ``normalise_depth`` sees the values in point
        order, so an ``auto`` vertical convention is inferred from the same
        first value.
        """
        self._require_open()
        points = list(points)
        if not points or not self.tile_cells or self.dx <= 0 or self.dy <= 0:
            return [self.sample(point, method) for point in points]
        count = len(points)
        px = np.fromiter((p.x() for p in points), dtype=np.float64, count=count)
        py = np.fromiter((p.y() for p in points), dtype=np.float64, count=count)
        extent = self.extent
        xmin, ymax = extent.xMinimum(), extent.yMaximum()
        ok = ((px >= xmin) & (px <= extent.xMaximum())
              & (py >= extent.yMinimum()) & (py <= ymax))
        # Points outside (or NaN) are answered None; park them off-grid so
        # they read no tiles and cast cleanly to int.
        x = np.where(ok, (px - xmin) / self.dx - .5, -2.0)
        y = np.where(ok, (ymax - py) / self.dy - .5, -2.0)
        if (method or self.options.get('sampling')) == 'nearest':
            values = self._cells(np.floor(x + .5).astype(np.int64),
                                 np.floor(y + .5).astype(np.int64))
            ok &= ~np.isnan(values)
        else:
            c, r = np.floor(x), np.floor(y)
            fx, fy = x - c, y - r
            cols, rows = c.astype(np.int64), r.astype(np.int64)
            values = np.zeros(count)
            for dc, dr, w in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                              (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
                used = w > 1e-12
                z = self._cells(cols + dc, rows + dr)
                ok &= ~(used & np.isnan(z))
                values = values + np.where(used, w * z, 0.0)
        return [normalise_depth(float(v), self.options) if good else None
                for v, good in zip(values.tolist(), ok.tolist())]


def configure_layers(parent, layers=None):
    """Project-persisted options shared by all bathymetry consumers."""
    from qgis.PyQt.QtWidgets import (QDialog, QVBoxLayout, QTableWidget,
        QTableWidgetItem, QComboBox, QDoubleSpinBox, QDialogButtonBox, QLabel)
    from .qgis_compat import BUTTON_BOX_OK, BUTTON_BOX_CANCEL
    layers = list(layers or QgsProject.instance().mapLayers().values())
    layers = [l for l in layers if isinstance(l, QgsRasterLayer) or
              (hasattr(l, 'geometryType') and l.geometryType() == 1)]
    dlg = QDialog(parent)
    dlg.setWindowTitle('Bathymetry source conventions (shared by profile tools)')
    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel('Set the vertical convention explicitly for nearshore or mixed-sign data.\n'
                           'Depths are converted to metres, positive down. Datum labels document the source; no datum conversion is applied.\n'
                           'For older mosaics set Native cell ≥ the coarsest input; new plugin mosaics read their native sources.'))
    table = QTableWidget(len(layers), 6)
    table.setHorizontalHeaderLabels(['Layer', 'Vertical values', 'Z units', 'Datum', 'Sampling', 'Native cell (m)'])
    layout.addWidget(table)
    for row, layer in enumerate(layers):
        opts = layer_options(layer)
        item = QTableWidgetItem(layer.name())
        from qgis.PyQt.QtCore import Qt
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, 0, item)
        for col, key, entries in [(1, 'vertical', [('Auto (offshore)', 'auto'), ('Depth, positive down', 'depth'), ('Elevation, positive up', 'elevation')]),
                                  (2, 'units', [('Metres', 'm'), ('Feet', 'ft')]),
                                  (4, 'sampling', [('Bilinear', 'bilinear'), ('Raw nearest cell', 'nearest')])]:
            combo = QComboBox()
            for label, value in entries:
                combo.addItem(label, value)
            combo.setCurrentIndex(max(0, combo.findData(opts[key])))
            table.setCellWidget(row, col, combo)
        table.setItem(row, 3, QTableWidgetItem(str(opts['datum'])))
        spin = QDoubleSpinBox(); spin.setRange(0, 100000); spin.setDecimals(3)
        spin.setValue(float(opts['native_cell_m'] or 0)); spin.setSpecialValueText('Automatic')
        table.setCellWidget(row, 5, spin)
    buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
    buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons); dlg.resize(1000, 450)
    if not dlg.exec():
        return False
    for row, layer in enumerate(layers):
        previous = layer_options(layer)
        changes = {}
        for col, key in [(1, 'vertical'), (2, 'units'), (4, 'sampling')]:
            changes[key] = table.cellWidget(row, col).currentData()
        changes['datum'] = table.item(row, 3).text()
        changes['native_cell_m'] = table.cellWidget(row, 5).value()
        for key,value in changes.items():
            if value != previous[key]:
                layer.setCustomProperty(PREFIX + key,value)
    QgsProject.instance().setDirty(True)
    return True
