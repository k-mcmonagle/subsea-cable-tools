"""Isolate native GeoJSON parsing and GeoPackage writing from the QGIS GUI.

Run with the QGIS Python interpreter. stdout is reserved for the JSON result.
"""

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--table', required=True)
    parser.add_argument('--crs', required=True)
    parser.add_argument('--prefix', required=True)
    args = parser.parse_args(argv)
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    app = None
    source = None
    verified = None
    try:
        from qgis.core import (
            QgsApplication, QgsCoordinateReferenceSystem,
            QgsCoordinateTransformContext, QgsVectorLayer,
        )

        QgsApplication.setPrefixPath(args.prefix, True)
        app = QgsApplication([], False)
        app.initQgis()

        # Load the shared writer under a private package name, without loading
        # the plugin UI or relying on the installed plugin directory's name.
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            '_subsea_mdb_conversion', root / '__init__.py',
            submodule_search_locations=[str(root)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        from _subsea_mdb_conversion.gpkg_writer import write_layer_to_gpkg

        crs = QgsCoordinateReferenceSystem(args.crs)
        if not crs.isValid():
            raise ValueError('Invalid source CRS for MDB conversion')
        source = QgsVectorLayer(args.input, args.table, 'ogr')
        if not source.isValid():
            raise ValueError('Could not open MDB worker GeoJSON')
        source.setCrs(crs)
        error, renamed = write_layer_to_gpkg(
            source, args.output, args.table, QgsCoordinateTransformContext(),
            fid_name='__subsea_fid')
        if error:
            raise RuntimeError(error)
        source_count = source.featureCount()
        source = None
        verified = QgsVectorLayer(args.output + '|layername=' + args.table, args.table, 'ogr')
        if not verified.isValid():
            raise RuntimeError('Converted GeoPackage could not be reopened')
        count = 0
        for feature in verified.getFeatures():
            geometry = feature.geometry()
            if not geometry.isNull():
                # Extremely large but finite coordinates can overflow native
                # extent, indexing and rendering calculations.
                for vertex in geometry.vertices():
                    if not all(math.isfinite(v) and abs(v) <= 1e12
                               for v in (vertex.x(), vertex.y())):
                        raise ValueError('Converted geometry contains unsafe coordinates')
                geometry.boundingBox()
            count += 1
        if count != source_count:
            raise RuntimeError(f'GeoPackage verification read {count} of {source_count} features')
        result = {'renamed_fields': renamed, 'verified': True, 'feature_count': count}
    except Exception as exc:
        sys.stderr.write(json.dumps({'error': str(exc), 'traceback': traceback.format_exc()}))
        return 1
    finally:
        # Release provider handles before tearing down the provider registry.
        source = None
        verified = None
        if app is not None:
            app.exitQgis()
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
