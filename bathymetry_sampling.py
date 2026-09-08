"""Shared QGIS bathymetry sampling and explicit per-layer conventions.

Values returned to profile consumers are metres, positive down. Bilinear
sampling uses native cell centres and requires every contributing cell.
"""
import json
import math
import os
from collections import OrderedDict

from qgis.core import QgsPointXY, QgsRasterLayer, QgsProject, QgsUnitTypes
from .kp_range_utils import make_distance_area
from .slope_utils import is_finite
from .qgis_compat import DISTANCE_METERS

PREFIX = 'subsea/bathymetry/'
VERSION = 'supported-bilinear-v3-terrace-baseline'


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


def native_cell_m(layer):
    override = float(layer_options(layer).get('native_cell_m') or 0)
    da = make_distance_area(layer.crs(), QgsProject.instance().transformContext())
    c = layer.extent().center()
    dx = abs(layer.rasterUnitsPerPixelX())
    dy = abs(layer.rasterUnitsPerPixelY())
    if layer.crs().isGeographic():
        x = da.measureLine(c, QgsPointXY(c.x() + dx, c.y()))
        y = da.measureLine(c, QgsPointXY(c.x(), c.y() + dy))
    else:
        factor = metres_per_unit(layer.crs())
        x, y = dx * factor, dy * factor
    return max(float(x), float(y), override)


def metres_per_unit(crs):
    return QgsUnitTypes.fromUnitToUnitFactor(crs.mapUnits(), DISTANCE_METERS)


def expand_rasters(layers, seen=None):
    """New mosaics carry a sidecar; engineering reads their native sources.

    A missing native file fails explicitly rather than trusting upsampled
    output pixels as native resolution. Ordinary/legacy rasters are retained.
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
        out.extend(expand_rasters(children, seen | {canonical}))
    # Stable finest-first across all consumers. Canonical URI is provenance.
    return sorted(out, key=native_cell_m)


class RasterSampler:
    def __init__(self, layer, band=1, clone=False):
        self.layer = layer  # retain lifetime, including expanded native layers
        self.provider = layer.dataProvider().clone() if clone else layer.dataProvider()
        self.options = layer_options(layer)
        self.band = band
        self.source_id = layer.source()
        self.cell_m = native_cell_m(layer)
        self.extent = self.provider.extent()
        self.nx, self.ny = self.provider.xSize(), self.provider.ySize()
        self.dx = self.extent.width() / self.nx if self.nx else 0
        self.dy = self.extent.height() / self.ny if self.ny else 0
        self.cache = OrderedDict()

    def _cell(self, col, row):
        if col < 0 or row < 0 or col >= self.nx or row >= self.ny:
            return None
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

    def sample(self, point, method=None):
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
