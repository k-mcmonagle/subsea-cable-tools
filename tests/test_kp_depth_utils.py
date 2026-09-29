# -*- coding: utf-8 -*-
"""QGIS checks for the KP Mouse tool's multi-source depth sampler.

* Nearest contour: the contour index stores geometries, so the nearest
  contour is the geometrically nearest one — not the one whose bounding box
  is nearest (a long diagonal contour whose box contains the point used to
  win over the contour right beside it).
* Profiles sample rasters through the batch sampler and must equal the
  per-point samples, raw (nearest) series included.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsPointXY,
                       QgsProject, QgsRasterLayer, QgsVectorLayer)

from ..bathymetry_sampling import PREFIX
from ..kp_range_utils import make_distance_area
from ..maptools.kp_depth_utils import DepthSampler

_CRS = "EPSG:32630"


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _contours(rows):
    layer = QgsVectorLayer("LineString?crs=%s&field=depth:double" % _CRS, "contours", "memory")
    feats = []
    for wkt, depth in rows:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry.fromWkt(wkt))
        feat.setAttributes([depth])
        feats.append(feat)
    layer.dataProvider().addFeatures(feats)
    layer.updateExtents()
    layer.setCustomProperty(PREFIX + "vertical", "depth")
    return layer


def test_nearest_contour_is_geometric_nearest():
    x0, y0 = 500000.0, 6000000.0
    layer = _contours([
        # Long diagonal: its bounding box contains the query point (bbox
        # distance 0) but the line itself is ~570 m away.
        ("LINESTRING(%f %f, %f %f)" % (x0, y0, x0 + 1000, y0 + 1000), 200.0),
        # Short contour 50 m from the query point (bbox distance 50 m).
        ("LINESTRING(%f %f, %f %f)" % (x0 + 850, y0 + 50, x0 + 950, y0 + 50), 45.0),
    ])
    sampler = DepthSampler(QgsCoordinateReferenceSystem(_CRS), [(layer, "depth")])
    depth = sampler.sample_point(QgsPointXY(x0 + 900, y0 + 100))
    return _result("nearest contour by geometry, not bounding box", depth == 45.0,
                   "depth %s (bbox-nearest would give 200)" % depth)


def test_profile_batch_equals_point_samples():
    import numpy as np
    from osgeo import gdal, osr
    gdal.UseExceptions()
    temp = Path(tempfile.mkdtemp(prefix="sct_kpdepth_"))
    try:
        path = temp / "grid.tif"
        rows, cols = 200, 300
        r, c = np.mgrid[0:rows, 0:cols]
        array = (80.0 + 0.05 * c + 0.03 * r + np.sin(c / 7.0)).astype(np.float32)
        array[90:100, 140:160] = -9999.0
        ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((400000.0, 5.0, 0, 6100000.0, 0, -5.0))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32630)
        ds.SetProjection(srs.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(array)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds = None
        layer = QgsRasterLayer(str(path), "grid")
        crs = QgsCoordinateReferenceSystem(_CRS)
        sampler = DepthSampler(crs, [(layer, "")])
        da = make_distance_area(crs, QgsProject.instance().transformContext())
        # Crosses the no-data hole (x 400700-400800, y 6099500-6099550).
        start, end = QgsPointXY(400020.0, 6099300.0), QgsPointXY(401480.0, 6099760.0)
        profile = sampler.profile(start, end, da)
        series = profile["rasters"][0]
        length = profile["length_m"]
        ok = True
        reference = DepthSampler(crs, [(layer, "")])
        src = reference._rasters[0]
        for x, y, raw in zip(series["x"], series["y"], series["raw_y"]):
            t = x / length
            p = QgsPointXY(start.x() + t * (end.x() - start.x()), start.y() + t * (end.y() - start.y()))
            ok = ok and reference._sample_raster(src, p) == y
            ok = ok and reference._sample_raster(src, p, "nearest") == raw
        gaps = sum(1 for v in series["y"] if v is None)
        return _result("profile batch sampling == per-point sampling", ok and 0 < gaps < len(series["y"]),
                       "%d stations, %d in the no-data hole" % (len(series["y"]), gaps))
    finally:
        import gc
        gc.collect()
        shutil.rmtree(temp, ignore_errors=True)


def run_all():
    return [
        test_nearest_contour_is_geometric_nearest(),
        test_profile_batch_equals_point_samples(),
    ]
