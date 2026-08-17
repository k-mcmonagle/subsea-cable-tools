# -*- coding: utf-8 -*-
"""Checks for the MBES XYZ raster tools (create from XYZ + merge).

Covers the pure helpers (format sniffing, grid-size detection, cell-centred
extents), an end-to-end run of Create Raster from XYZ over two files with
different grid sizes and delimiters/headers in one batch, value fidelity of
the burnt cells, and that Merge MBES Rasters keeps the finest resolution.

Requires the QGIS API plus the GDAL Processing provider
(run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

import math
import os
import tempfile

from qgis.core import (
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRasterLayer,
)

from ..processing.create_mbes_raster_from_xyz_algorithm import (
    CreateMBESRasterFromXYZAlgorithm,
    cell_centred_extent,
    detect_grid_size,
    sniff_xyz_format,
)
from ..processing.merge_mbes_rasters_algorithm import MergeMBESRastersAlgorithm

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


def _init_processing() -> bool:
    try:
        import sys

        from qgis.core import QgsApplication

        import qgis

        candidates = [
            os.path.join(QgsApplication.prefixPath(), "python", "plugins"),
            os.path.join(os.path.dirname(os.path.dirname(qgis.__file__)), "plugins"),
        ]
        for plugins_dir in candidates:
            if os.path.isdir(plugins_dir) and plugins_dir not in sys.path:
                # Ahead of the working directory: when cwd is this plugin's
                # folder, its own `processing` package would shadow QGIS's
                # processing plugin.
                sys.path.insert(0, plugins_dir)
        from processing.core.Processing import Processing

        Processing.initialize()
        return QgsApplication.processingRegistry().algorithmById("gdal:rasterize") is not None
    except Exception as exc:  # pragma: no cover
        print(f"[SKIP] processing framework unavailable: {exc}")
        return False


def _write_tile(path, x0, y0, n, step, depth_fn, delimiter=" ", header=None):
    lines = [] if header is None else [header]
    for j in range(n):
        for i in range(n):
            x = x0 + i * step
            y = y0 + j * step
            lines.append(delimiter.join(
                [f"{x:.3f}", f"{y:.3f}", f"{depth_fn(i, j):.3f}"]))
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def _sample(raster_path, x, y):
    layer = QgsRasterLayer(raster_path, os.path.basename(raster_path), "gdal")
    if not layer.isValid():
        return None, None
    value, ok = layer.dataProvider().sample(QgsPointXY(x, y), 1)
    return layer, (value if ok else None)


def test_pure_helpers() -> bool:
    folder = tempfile.mkdtemp(prefix="mbes_sniff_")
    path = os.path.join(folder, "probe.xyz")
    with open(path, "w") as handle:
        handle.write("# survey X123\nEasting,Northing,Depth\n500000.0,6000000.0,-12.5\n")
    delim, skip = sniff_xyz_format(path)
    ok = delim == "," and skip == 1

    path2 = os.path.join(folder, "probe2.xyz")
    with open(path2, "w") as handle:
        handle.write("500000.0 6000000.0 -12.5\n500001.0 6000000.0 -12.6\n")
    delim2, skip2 = sniff_xyz_format(path2)
    ok = ok and delim2 is None and skip2 == 0

    if np is not None:
        xs = np.array([0.0, 0.5, 1.0, 2.0, 2.5])   # 0.5 grid with a missing cell
        ys = np.array([10.0, 10.5, 11.0, 11.5, 12.0])
        grid = detect_grid_size(xs, ys)
        ok = ok and abs(grid - 0.5) < 1e-9
        extent = cell_centred_extent(xs, ys, 0.5)
        ok = ok and abs(extent.xMinimum() - (-0.25)) < 1e-9
        ok = ok and abs(extent.xMaximum() - 2.75) < 1e-9
        ok = ok and abs(round(extent.width() / 0.5) - 6) < 1e-9
    return _result("pure helpers: sniff, grid detect, cell-centred extent", ok)


def test_multifile_create_and_merge() -> bool:
    if np is None:
        return _result("multi-file create + merge", False, "NumPy unavailable")
    if not _init_processing():
        return _result("multi-file create + merge", True,
                       "skipped: GDAL processing provider unavailable")

    folder = tempfile.mkdtemp(prefix="mbes_xyz_")
    # Tile A: 1 m grid, space-delimited with a header line.
    tile_a = os.path.join(folder, "tile_a.xyz")
    _write_tile(tile_a, 500000.0, 6000000.0, 12, 1.0,
                lambda i, j: -(10.0 + i + 0.1 * j),
                delimiter=" ", header="Easting Northing Depth")
    # Tile B: 2 m grid, comma-delimited, adjacent to the east.
    tile_b = os.path.join(folder, "tile_b.xyz")
    _write_tile(tile_b, 500020.0, 6000000.0, 6, 2.0,
                lambda i, j: -(40.0 + i + 0.1 * j), delimiter=",")

    out_folder = os.path.join(folder, "rasters")
    algorithm = CreateMBESRasterFromXYZAlgorithm()
    algorithm.initAlgorithm()
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    from qgis import processing

    processing.run(algorithm, {
        "INPUT_XYZ": [tile_a, tile_b],
        "CRS": "EPSG:32631",
        "GRID_SIZE": 0.0,
        "MAX_DISTANCE": 0.0,
        "METHOD": 0,
        "OUTPUT": out_folder,
        "COMPRESS": True,
    }, context=context, feedback=feedback)

    raster_a = os.path.join(out_folder, "tile_a.tif")
    raster_b = os.path.join(out_folder, "tile_b.tif")
    ok = os.path.isfile(raster_a) and os.path.isfile(raster_b)
    if not ok:
        return _result("multi-file create + merge", False, "outputs missing")

    layer_a, value_a = _sample(raster_a, 500003.0, 6000002.0)   # i=3, j=2
    layer_b, value_b = _sample(raster_b, 500024.0, 6000004.0)   # i=2, j=2
    ok = ok and layer_a is not None and abs(layer_a.rasterUnitsPerPixelX() - 1.0) < 1e-6
    ok = ok and layer_b is not None and abs(layer_b.rasterUnitsPerPixelX() - 2.0) < 1e-6
    ok = ok and value_a is not None and abs(value_a - (-(10.0 + 3 + 0.2))) < 1e-4
    ok = ok and value_b is not None and abs(value_b - (-(40.0 + 2 + 0.2))) < 1e-4
    detail = (f"a: {layer_a.rasterUnitsPerPixelX():g} m/px z={value_a}; "
              f"b: {layer_b.rasterUnitsPerPixelX():g} m/px z={value_b}")

    # --- merge keeps the finest resolution, whichever order is given ---
    project = QgsProject.instance()
    lyr_coarse = QgsRasterLayer(raster_b, "tile_b", "gdal")
    lyr_fine = QgsRasterLayer(raster_a, "tile_a", "gdal")
    project.addMapLayers([lyr_coarse, lyr_fine], False)
    merged_path = os.path.join(folder, "merged.tif")
    merge = MergeMBESRastersAlgorithm()
    merge.initAlgorithm()
    processing.run(merge, {
        "INPUTS": [lyr_coarse, lyr_fine],     # coarse FIRST: the old tool broke here
        "OUTPUT": merged_path,
        "COMPRESS": True,
    }, context=context, feedback=feedback)

    merged, merged_a = _sample(merged_path, 500003.0, 6000002.0)
    _, merged_b = _sample(merged_path, 500024.0, 6000004.0)
    ok = ok and merged is not None and abs(merged.rasterUnitsPerPixelX() - 1.0) < 1e-6
    ok = ok and merged_a is not None and abs(merged_a - (-(10.0 + 3 + 0.2))) < 1e-4
    ok = ok and merged_b is not None and abs(merged_b - (-(40.0 + 2 + 0.2))) < 1e-4
    # the gap between the tiles stays NoData/transparent
    _, gap = _sample(merged_path, 500016.0, 6000015.0)
    ok = ok and (gap is None or gap <= -9998.0 or math.isnan(gap))
    detail += f"; merged: {merged.rasterUnitsPerPixelX():g} m/px"
    project.removeMapLayers([lyr_coarse.id(), lyr_fine.id()])
    return _result("multi-file create + merge keeps per-file resolution", ok, detail)


def run_all():
    return [
        test_pure_helpers(),
        test_multifile_create_and_merge(),
    ]


if __name__ == "__main__":
    import sys

    sys.exit(0 if all(run_all()) else 1)
