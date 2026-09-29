# -*- coding: utf-8 -*-
"""QGIS checks for the shared bathymetry ``RasterSampler``.

The sampler reads cells in tiles (one ``provider.block()`` per 256 x 256
cells) instead of one ``provider.sample()`` per cell. These checks build
synthetic GeoTIFFs — no-data holes, partial edge tiles, non-square cells,
scaled integers, user no-data ranges, a geographic grid — and require the
tiled path and the batch API to return exactly what the per-cell path
(``tile_cells=0``, the previous implementation) returns.
"""

from __future__ import annotations

import math
import random
import shutil
import tempfile
import time
from pathlib import Path

from qgis.core import QgsPointXY, QgsRasterLayer, QgsRasterRange

from ..bathymetry_sampling import RasterSampler


def _result(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""))
    return ok


def _write_tif(path, array, geotransform, epsg, gdal_type, nodata=None,
               scale=None, offset=None):
    from osgeo import gdal, osr
    gdal.UseExceptions()
    rows, cols = array.shape
    ds = gdal.GetDriverByName("GTiff").Create(str(path), cols, rows, 1, gdal_type)
    ds.SetGeoTransform(geotransform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(array)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    if scale is not None:
        band.SetScale(scale)
    if offset is not None:
        band.SetOffset(offset)
    ds.FlushCache()
    ds = None


def _surface(rows, cols, seed):
    import numpy as np
    rng = np.random.default_rng(seed)
    r, c = np.mgrid[0:rows, 0:cols]
    return (120.0 + 0.037 * c - 0.021 * r + 3.0 * np.sin(c / 17.0) * np.cos(r / 11.0)
            + rng.normal(0.0, 0.05, (rows, cols)))


def _punch_holes(array, value, seed):
    """Scattered single cells, a block, half the last row and the column on
    a tile boundary become no-data."""
    import numpy as np
    rng = np.random.default_rng(seed)
    rows, cols = array.shape
    flat = rng.choice(rows * cols, size=max(1, rows * cols // 40), replace=False)
    array.flat[flat] = value
    array[rows // 3:rows // 3 + 9, cols // 2:cols // 2 + 13] = value
    array[rows - 1, :cols // 2] = value
    array[:, min(255, cols - 1)] = value
    return array


def _probe_points(sampler, count, seed):
    """Random points plus the awkward ones: cell centres, cell edges, tile
    edges, the raster edges/corners and points just outside."""
    rng = random.Random(seed)
    e = sampler.extent
    dx, dy = sampler.dx, sampler.dy
    pts = [QgsPointXY(rng.uniform(e.xMinimum(), e.xMaximum()),
                      rng.uniform(e.yMinimum(), e.yMaximum())) for _ in range(count)]
    for col in (0, 1, 127, 128, 255, 256, 257, sampler.nx - 2, sampler.nx - 1):
        for row in (0, 1, 127, 128, 255, 256, sampler.ny - 1):
            if 0 <= col < sampler.nx and 0 <= row < sampler.ny:
                pts.append(QgsPointXY(e.xMinimum() + (col + .5) * dx, e.yMaximum() - (row + .5) * dy))
                pts.append(QgsPointXY(e.xMinimum() + col * dx, e.yMaximum() - row * dy))
                pts.append(QgsPointXY(e.xMinimum() + (col + .25) * dx, e.yMaximum() - (row + .75) * dy))
    for x, y in ((e.xMinimum(), e.yMinimum()), (e.xMaximum(), e.yMaximum()),
                 (e.xMinimum(), e.center().y()), (e.xMaximum(), e.center().y()),
                 (e.xMinimum() - dx * .01, e.center().y()), (e.center().x(), e.yMaximum() + dy),
                 (float("nan"), e.center().y())):
        pts.append(QgsPointXY(x, y))
    return pts


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return a == b or (math.isnan(a) and math.isnan(b))


def _compare(layer, points, label, expect_tiles=True):
    """Per-cell reference vs tiled scalar vs tiled batch, both methods.

    ``expect_tiles`` also requires the tiled path to have stayed in use, so
    a silent fall-back to per-cell reads cannot pass trivially.
    """
    detail = []
    ok = True
    for method in ("bilinear", "nearest"):
        reference = RasterSampler(layer, tile_cells=0)
        tiled = RasterSampler(layer)
        batch = RasterSampler(layer)
        ref = [reference.sample(p, method) for p in points]
        got = [tiled.sample(p, method) for p in points]
        many = batch.sample_many(points, method)
        bad = sum(1 for a, b in zip(ref, got) if not _same(a, b))
        bad_many = sum(1 for a, b in zip(ref, many) if not _same(a, b))
        valid = sum(1 for v in ref if v is not None)
        ok = ok and bad == 0 and bad_many == 0 and 0 < valid < len(points)
        ok = ok and bool(tiled.tile_cells and batch.tile_cells) == expect_tiles
        detail.append("%s: %d/%d valid, %d scalar + %d batch mismatches, tiles %s"
                      % (method, valid, len(points), bad, bad_many,
                         "on" if tiled.tile_cells else "OFF (per-cell fallback)"))
    return _result("tiled sampling == per-cell: " + label, ok, "; ".join(detail))


def test_float32_nodata_holes_partial_tiles(temp):
    import numpy as np
    from osgeo import gdal
    rows, cols = 530, 700            # partial tiles on both axes
    array = _punch_holes(_surface(rows, cols, 1).astype(np.float32), -9999.0, 2)
    path = temp / "float32.tif"
    # Non-square cells and an origin that is not a multiple of the cell size.
    _write_tif(path, array, (500123.37, 7.3, 0, 6100456.91, 0, -4.9), 32630,
               gdal.GDT_Float32, nodata=-9999.0)
    layer = QgsRasterLayer(str(path), "float32")
    sampler = RasterSampler(layer)
    return _compare(layer, _probe_points(sampler, 3000, 3), "Float32, holes, partial tiles")


def test_scaled_int16(temp):
    import numpy as np
    from osgeo import gdal
    rows, cols = 300, 290
    array = np.round(_surface(rows, cols, 4) * 10.0).astype(np.int16)
    array = _punch_holes(array, -32768, 5)
    path = temp / "int16.tif"
    _write_tif(path, array, (-1234.5, 2.0, 0, 5555.25, 0, -2.0), 32631,
               gdal.GDT_Int16, nodata=-32768, scale=0.1, offset=-3.7)
    layer = QgsRasterLayer(str(path), "int16")
    sampler = RasterSampler(layer)
    # Scaled bands stay on per-cell reads: QGIS rounds scaled blocks to
    # Float32 but sample() keeps double precision.
    return _compare(layer, _probe_points(sampler, 2000, 6),
                    "scaled Int16 (0.1, -3.7) with no-data, per-cell by design",
                    expect_tiles=False)


def test_float64_nan_and_user_nodata(temp):
    import numpy as np
    from osgeo import gdal
    rows, cols = 260, 520
    array = _surface(rows, cols, 7)
    array[::37, ::23] = np.nan
    array[100:110, 300:340] = 125.0      # a user no-data band
    path = temp / "float64.tif"
    _write_tif(path, array, (10.0, 1.5, 0, 20.0, 0, -1.5), 32632, gdal.GDT_Float64)
    layer = QgsRasterLayer(str(path), "float64")
    provider = layer.dataProvider()
    provider.setUserNoDataValue(1, [QgsRasterRange(124.99, 125.01),
                                    QgsRasterRange(130.0, 131.0, QgsRasterRange.Exclusive)])
    sampler = RasterSampler(layer)
    return _compare(layer, _probe_points(sampler, 2000, 8), "Float64 with NaN + user no-data ranges")


def test_geographic_grid(temp):
    import numpy as np
    from osgeo import gdal
    rows, cols = 400, 330
    array = _punch_holes(_surface(rows, cols, 9).astype(np.float32), -3.4028234663852886e+38, 10)
    path = temp / "geographic.tif"
    _write_tif(path, array, (-3.123456, 0.000833333333, 0, 55.987654, 0, -0.000555555556),
               4326, gdal.GDT_Float32, nodata=-3.4028234663852886e+38)
    layer = QgsRasterLayer(str(path), "geographic")
    sampler = RasterSampler(layer)
    return _compare(layer, _probe_points(sampler, 2000, 11), "geographic Float32 (float-max no-data)")


def test_auto_vertical_inference_order(temp):
    """``auto`` infers elevation/depth from the first value it sees; the
    batch API must present values in point order like the scalar loop."""
    import numpy as np
    from osgeo import gdal
    array = np.full((40, 40), -50.0, dtype=np.float32)
    array[:, 20:] = 30.0                 # mixed signs: order decides
    path = temp / "mixed.tif"
    _write_tif(path, array, (0.0, 1.0, 0, 40.0, 0, -1.0), 32630, gdal.GDT_Float32, nodata=-9999.0)
    layer = QgsRasterLayer(str(path), "mixed")
    points = [QgsPointXY(30.5, 20.5), QgsPointXY(5.5, 20.5), QgsPointXY(35.5, 2.5)]
    scalar = RasterSampler(layer)
    batch = RasterSampler(layer)
    ref = [scalar.sample(p) for p in points]
    many = batch.sample_many(points)
    ok = ref == many and ref[0] == 30.0 and ref[1] == -50.0
    return _result("batch keeps the auto vertical-convention inference order", ok, "%s vs %s" % (ref, many))


def _big_layer(temp):
    import numpy as np
    from osgeo import gdal
    array = _surface(1024, 1024, 12).astype(np.float32)
    path = temp / "big.tif"
    if not path.exists():
        _write_tif(path, array, (0.0, 1.0, 0, 1024.0, 0, -1.0), 32630, gdal.GDT_Float32, nodata=-9999.0)
    return QgsRasterLayer(str(path), "big")


def test_tile_cache_is_bounded(temp):
    layer = _big_layer(temp)
    probe = RasterSampler(layer)
    budget = 3 * probe.tile_cells * probe.tile_cells * 8
    sampler = RasterSampler(layer, cache_bytes=budget)
    points = [QgsPointXY(col + .5, 1024 - row - .5)
              for row in range(0, 1024, 64) for col in range(0, 1024, 64)]
    sampler.sample_many(points, "nearest")
    ok = sampler.tile_cells > 0 and len(sampler._tiles) <= 3 and sampler._tile_bytes <= budget
    return _result("tile cache stays within its memory budget", ok,
                   "%d tiles, %d bytes (budget %d)" % (len(sampler._tiles), sampler._tile_bytes, budget))


def test_scattered_scalar_queries_fall_back_to_cells(temp):
    """Scattered scalar queries over more tiles than the cache holds would
    re-read a whole tile per cell; the sampler notices and returns scalar
    reads to per-cell, while a coherent profile keeps its tiles."""
    layer = _big_layer(temp)
    probe = RasterSampler(layer)
    budget = 2 * probe.tile_cells * probe.tile_cells * 8
    rng = random.Random(15)
    scattered = [QgsPointXY(rng.uniform(0, 1024), rng.uniform(0, 1024)) for _ in range(600)]
    line = [QgsPointXY(3.0 + 1018.0 * i / 3000, 3.0 + 600.0 * i / 3000) for i in range(3000)]
    reference = RasterSampler(layer, tile_cells=0)
    thrashed = RasterSampler(layer, cache_bytes=budget)
    coherent = RasterSampler(layer, cache_bytes=budget)
    same = ([thrashed.sample(p) for p in scattered] == [reference.sample(p) for p in scattered]
            and [coherent.sample(p) for p in line] == [reference.sample(p) for p in line])
    ok = same and not thrashed._scalar_tiles and coherent._scalar_tiles
    return _result("scattered scalar queries fall back to per-cell; profiles keep tiles", ok,
                   "scattered tiles=%s, profile tiles=%s, identical=%s"
                   % (thrashed._scalar_tiles, coherent._scalar_tiles, same))


def test_timing(temp):
    """Rough timing on a 2000 x 1500 grid: a dense bilinear profile (mostly
    cache hits for the per-cell path) and scattered queries (mostly misses,
    like cross-slope and risk scans)."""
    import numpy as np
    from osgeo import gdal
    array = _surface(1500, 2000, 13).astype(np.float32)
    path = temp / "timing.tif"
    _write_tif(path, array, (400000.0, 2.0, 0, 6000000.0, 0, -2.0), 32630, gdal.GDT_Float32, nodata=-9999.0)
    layer = QgsRasterLayer(str(path), "timing")
    count = 20000
    line = [QgsPointXY(400010.0 + 3980.0 * i / count, 5999990.0 - 2980.0 * i / count)
            for i in range(count)]
    rng = random.Random(14)
    scattered = [QgsPointXY(rng.uniform(400000.0, 404000.0), rng.uniform(5997000.0, 6000000.0))
                 for _ in range(5000)]
    ok = True
    details = []
    for name, points in (("20k-station profile", line), ("5k scattered points", scattered)):
        timings, results = {}, {}
        for label, sampler, batch in (
                ("per-cell", RasterSampler(layer, tile_cells=0), False),
                ("tiled", RasterSampler(layer), False),
                ("batch", RasterSampler(layer), True)):
            start = time.perf_counter()
            results[label] = (sampler.sample_many(points) if batch
                              else [sampler.sample(p) for p in points])
            timings[label] = time.perf_counter() - start
        ok = ok and results["per-cell"] == results["tiled"] == results["batch"]
        details.append("%s: per-cell %.3f s, tiled %.3f s (x%.1f), batch %.3f s (x%.1f)" % (
            name, timings["per-cell"], timings["tiled"],
            timings["per-cell"] / max(timings["tiled"], 1e-9), timings["batch"],
            timings["per-cell"] / max(timings["batch"], 1e-9)))
    return _result("identical results, rough timing", ok, "; ".join(details))


def test_cloned_provider_is_released(temp):
    """``clone=True`` used to keep a C++-owned provider clone forever, so
    its GDAL handle held the file locked on Windows until QGIS exited. The
    sampler now owns the clone: dropping it, or close(), frees the file."""
    import gc
    import os
    import numpy as np
    from osgeo import gdal
    ok, detail = True, []
    for how in ("drop", "close"):
        path = temp / ("release_%s.tif" % how)
        _write_tif(path, _surface(64, 64, 16).astype(np.float32), (0.0, 1.0, 0, 64.0, 0, -1.0),
                   32630, gdal.GDT_Float32, nodata=-9999.0)
        sampler = RasterSampler(QgsRasterLayer(str(path), how), clone=True)
        value = sampler.sample(QgsPointXY(10.5, 20.5))
        if how == "close":
            sampler.close()
            sampler.close()                      # twice is harmless
            try:
                sampler.sample(QgsPointXY(10.5, 20.5))
                closed_raises = False
            except RuntimeError:
                closed_raises = True
            ok = ok and closed_raises
        del sampler
        gc.collect()
        moved = temp / ("release_%s_moved.tif" % how)
        try:
            os.replace(str(path), str(moved))
            released = True
        except OSError:  # PermissionError: still open
            released = False
        ok = ok and value is not None and released
        detail.append("%s: file released=%s" % (how, released))
    return _result("cloned provider freed with the sampler (file not locked)", ok, "; ".join(detail))


def test_explicit_transform_context(temp):
    """Samplers built off the main thread pass a transform context instead
    of reading the (not thread-safe) current project; the cell size and
    samples are the same, including from a worker thread."""
    import threading
    import numpy as np
    from osgeo import gdal
    from qgis.core import QgsCoordinateTransformContext, QgsProject
    from ..bathymetry_sampling import expand_rasters, native_cell_m
    path = temp / "context.tif"
    _write_tif(path, _surface(80, 60, 17).astype(np.float32),
               (-3.0, 0.001, 0, 56.0, 0, -0.0006), 4326, gdal.GDT_Float32, nodata=-9999.0)
    layer = QgsRasterLayer(str(path), "context")
    context = QgsProject.instance().transformContext()
    default = native_cell_m(layer)
    explicit = native_cell_m(layer, QgsCoordinateTransformContext(context))
    ordered = expand_rasters([layer], transform_context=context)
    result = {}

    def worker():
        sampler = RasterSampler(layer, clone=True, transform_context=context)
        result["cell"] = sampler.cell_m
        result["value"] = sampler.sample(QgsPointXY(-2.97, 55.98))
        sampler.close()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    main = RasterSampler(layer).sample(QgsPointXY(-2.97, 55.98))
    ok = (abs(default - explicit) < 1e-9 and len(ordered) == 1
          and abs(result.get("cell", -1) - default) < 1e-9
          and main is not None and result.get("value") == main)
    return _result("transform context passed in: same cell size and samples (worker thread)", ok,
                   "cell %.3f / %.3f m, worker sample %s vs %s"
                   % (default, explicit, result.get("value"), main))


def run_all():
    temp = Path(tempfile.mkdtemp(prefix="sct_bathy_"))
    try:
        return [
            test_float32_nodata_holes_partial_tiles(temp),
            test_scaled_int16(temp),
            test_float64_nan_and_user_nodata(temp),
            test_geographic_grid(temp),
            test_auto_vertical_inference_order(temp),
            test_tile_cache_is_bounded(temp),
            test_scattered_scalar_queries_fall_back_to_cells(temp),
            test_cloned_provider_is_released(temp),
            test_explicit_transform_context(temp),
            test_timing(temp),
        ]
    finally:
        import gc
        gc.collect()
        shutil.rmtree(temp, ignore_errors=True)
