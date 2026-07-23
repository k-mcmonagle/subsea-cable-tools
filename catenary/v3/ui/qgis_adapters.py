# -*- coding: utf-8 -*-
"""QGIS integration helpers for the Cable Lay Simulator (3D).

All ``qgis`` imports live inside the functions so this module imports
cleanly outside QGIS (unit tests, headless use). NumPy is the only
top-level dependency.

Frame convention: the simulator works in a **local metric frame** whose
origin sits at ``origin_map_xy`` in the project's (projected) map CRS.
These adapters own the local <-> map mapping:

* :func:`sample_raster_bathymetry` samples depths around a map-CRS centre
  and returns a dict in local coordinates (centre = local ``(0, 0)``)
  that feeds ``GridBathymetry(x0, y0, dx, dy, depths)`` directly;
* :func:`push_chains_to_map` / :func:`push_markers_to_map` translate
  local results back to map coordinates and add memory layers.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np

from ..engine.bathymetry import fill_nodata_nearest


def _local_metric_crs(origin_map_xy: Tuple[float, float], map_crs, ctx):
    """A metric CRS matching the simulator's local frame: azimuthal
    equidistant centred on the origin (x = metres east, y = metres north).
    Valid anywhere on the planet, whatever the map CRS."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY

    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    ll = QgsCoordinateTransform(map_crs, wgs, ctx).transform(
        QgsPointXY(float(origin_map_xy[0]), float(origin_map_xy[1])))
    proj = (f"+proj=aeqd +lat_0={ll.y():.10f} +lon_0={ll.x():.10f} "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
    crs = None
    if hasattr(QgsCoordinateReferenceSystem, "fromProj"):
        crs = QgsCoordinateReferenceSystem.fromProj(proj)
    if crs is None or not crs.isValid():  # QGIS < 3.10.3 fallback
        crs = QgsCoordinateReferenceSystem()
        crs.createFromProj4(proj)
    if not crs.isValid():
        raise RuntimeError("Could not create the local metric (AEQD) CRS.")
    return crs


def local_frame_transforms(origin_map_xy: Tuple[float, float], crs_authid: str):
    """(to_map, to_local) coordinate transforms between the local metric
    frame and the CRS ``crs_authid``. All local -> map placement must go
    through these — adding metres to map coordinates directly is wrong in
    geographic or non-metre CRSs."""
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

    map_crs = QgsCoordinateReferenceSystem(crs_authid)
    if not map_crs.isValid():
        raise ValueError(f"Invalid CRS {crs_authid!r}.")
    ctx = QgsProject.instance().transformContext()
    local = _local_metric_crs(origin_map_xy, map_crs, ctx)
    from qgis.core import QgsCoordinateTransform as _T

    return _T(local, map_crs, ctx), _T(map_crs, local, ctx)


def map_points_to_local(points, origin_map_xy: Tuple[float, float],
                        crs_authid: str) -> List[Tuple[float, float]]:
    """Map-CRS (x, y) points -> local metric frame (metres E/N of origin)."""
    from qgis.core import QgsPointXY

    _, to_local = local_frame_transforms(origin_map_xy, crs_authid)
    out = []
    for x, y in points:
        p = to_local.transform(QgsPointXY(float(x), float(y)))
        out.append((float(p.x()), float(p.y())))
    return out


def local_points_to_map(points, origin_map_xy: Tuple[float, float],
                        crs_authid: str) -> List[Tuple[float, float]]:
    """Local metric (x, y) points -> map CRS."""
    from qgis.core import QgsPointXY

    to_map, _ = local_frame_transforms(origin_map_xy, crs_authid)
    out = []
    for x, y in points:
        p = to_map.transform(QgsPointXY(float(x), float(y)))
        out.append((float(p.x()), float(p.y())))
    return out


def list_raster_layers() -> List[Tuple[str, str]]:
    """(layer id, display name) for every raster layer in the project."""
    from qgis.core import QgsProject, QgsRasterLayer

    out: List[Tuple[str, str]] = []
    for layer_id, layer in QgsProject.instance().mapLayers().items():
        if isinstance(layer, QgsRasterLayer):
            out.append((layer_id, layer.name()))
    return out


def sample_raster_bathymetry(
    layer_id: str,
    center_map_xy: Tuple[float, float],
    half_extent_m: float,
    n: int = 80,
    band: int = 1,
    depths_positive_down: bool = True,
) -> dict:
    """Sample an ``n x n`` depth grid from a raster around a map-CRS centre.

    Sampling points are laid out on a regular metric grid in the local
    AEQD frame centred on ``center_map_xy`` (given in the project CRS,
    which may be geographic or projected in any unit), spanning
    ``+/- half_extent_m``, then transformed to the raster layer's CRS for
    the identify calls.

    Returns a dict with keys ``x0, y0, dx, dy, depths`` (2D list, positive
    down; row ``i`` at ``y0 + i*dy``, column ``j`` at ``x0 + j*dx``) in
    LOCAL coordinates (centre = local ``(0, 0)``, so ``x0 = -half_extent``),
    plus ``origin_map_xy`` (the centre) and ``crs_authid`` (project CRS).
    The dict feeds ``GridBathymetry(x0, y0, dx, dy, depths)`` directly.

    If ``depths_positive_down`` is False the raster is treated as holding
    negative-down elevations and values are negated. Nodata / failed
    samples are filled from the nearest valid cells; the returned dict
    includes ``nodata_fraction`` (0..1) so callers can warn the user.

    The raster is read as a single block covering the sample extent (one
    provider call) rather than one ``identify()`` per node, so sampling
    stays fast on large or networked rasters; a per-point identify loop is
    the fallback if the block read fails.
    """
    from qgis.core import (
        QgsCoordinateTransform,
        QgsPointXY,
        QgsProject,
        QgsRaster,
        QgsRasterLayer,
        QgsRectangle,
    )

    if n < 2:
        raise ValueError("n must be at least 2 for a usable grid.")
    if not (float(half_extent_m) > 0.0):
        raise ValueError("half_extent_m must be positive.")

    project = QgsProject.instance()
    project_crs = project.crs()
    if not project_crs.isValid():
        raise ValueError("The project has no valid CRS set.")

    layer = project.mapLayer(layer_id)
    if layer is None:
        raise ValueError(f"No layer with id {layer_id!r} in the project.")
    if not isinstance(layer, QgsRasterLayer):
        raise ValueError(f"Layer {layer.name()!r} is not a raster layer.")
    provider = layer.dataProvider()
    if provider is None:
        raise RuntimeError(f"Raster layer {layer.name()!r} has no data provider.")

    cx, cy = float(center_map_xy[0]), float(center_map_xy[1])
    try:
        local_crs = _local_metric_crs((cx, cy), project_crs, project.transformContext())
        transform = QgsCoordinateTransform(
            local_crs, layer.crs(), project.transformContext()
        )
    except Exception as exc:  # noqa: BLE001 - re-raise with context
        raise RuntimeError(
            f"Could not build the local-frame transform to "
            f"{layer.crs().authid()}: {exc}"
        ) from exc
    half = float(half_extent_m)
    n = int(n)
    step = 2.0 * half / (n - 1)
    xs_local = np.linspace(-half, half, n)
    ys_local = np.linspace(-half, half, n)

    # Transform the sample lattice to the raster CRS once.
    pts = np.full((n, n, 2), np.nan, dtype=float)
    for i, yl in enumerate(ys_local):
        for j, xl in enumerate(xs_local):
            try:
                p = transform.transform(QgsPointXY(float(xl), float(yl)))
                pts[i, j, 0] = p.x()
                pts[i, j, 1] = p.y()
            except Exception:
                continue  # outside transform validity -> leave as nodata

    depths = np.full((n, n), np.nan, dtype=float)
    ok = np.isfinite(pts).all(axis=2)
    block = None
    if np.any(ok):
        xmin = float(np.nanmin(pts[..., 0]))
        xmax = float(np.nanmax(pts[..., 0]))
        ymin = float(np.nanmin(pts[..., 1]))
        ymax = float(np.nanmax(pts[..., 1]))
        rect = QgsRectangle(xmin, ymin, xmax, ymax)
        rect.grow(max(rect.width(), rect.height()) * 0.01 + 1e-9)
        # Read finer than the sample lattice so nearest-pixel lookup is
        # faithful, but bounded so one call stays cheap.
        bw = bh = int(min(1024, max(64, 4 * n)))
        try:
            block = provider.block(band, rect, bw, bh)
            if block is None or not block.isValid() or block.width() < 1:
                block = None
        except Exception:
            block = None
    if block is not None:
        xres = rect.width() / block.width()
        yres = rect.height() / block.height()
        for i in range(n):
            for j in range(n):
                if not ok[i, j]:
                    continue
                col = int((pts[i, j, 0] - rect.xMinimum()) / xres)
                row = int((rect.yMaximum() - pts[i, j, 1]) / yres)
                col = min(max(col, 0), block.width() - 1)
                row = min(max(row, 0), block.height() - 1)
                if not block.isNoData(row, col):
                    depths[i, j] = float(block.value(row, col))
    else:
        # Fallback: per-point identify (slow, but always available).
        for i in range(n):
            for j in range(n):
                if not ok[i, j]:
                    continue
                point = QgsPointXY(float(pts[i, j, 0]), float(pts[i, j, 1]))
                result = provider.identify(point, QgsRaster.IdentifyFormatValue)
                if not result.isValid():
                    continue
                value = result.results().get(band)
                if value is None:
                    continue
                try:
                    depths[i, j] = float(value)
                except (TypeError, ValueError):
                    continue

    if not depths_positive_down:
        depths = -depths

    finite_mask = np.isfinite(depths)
    if not finite_mask.any():
        raise ValueError(
            f"No valid raster values sampled from {layer.name()!r} around "
            f"({cx:.1f}, {cy:.1f}) +/- {half:.0f} m — check the layer extent, "
            "band and CRS."
        )
    nodata_fraction = float(np.mean(~finite_mask))
    depths = fill_nodata_nearest(depths)

    return {
        "x0": -half,
        "y0": -half,
        "dx": step,
        "dy": step,
        "depths": depths.tolist(),
        "nodata_fraction": nodata_fraction,
        "origin_map_xy": (cx, cy),
        "crs_authid": project_crs.authid(),
    }


def push_chains_to_map(
    name: str,
    chains: Sequence[Tuple[str, "np.ndarray"]],
    origin_map_xy: Tuple[float, float],
    crs_authid: str,
):
    """Add local-frame chain polylines to the map as a LineStringZ layer.

    ``chains`` is (chain name, (n, 3) local xyz) tuples; the local metric
    vertices are georeferenced through the AEQD frame centred on
    ``origin_map_xy`` (so scale is correct in any CRS). Returns the layer.
    """
    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsLineString,
        QgsPoint,
        QgsProject,
        QgsVectorLayer,
    )
    from ....qgis_compat import FIELD_TYPE_STRING

    layer = QgsVectorLayer(f"LineStringZ?crs={crs_authid}", name, "memory")
    if not layer.isValid():
        raise RuntimeError(
            f"Could not create memory layer {name!r} (crs={crs_authid!r})."
        )
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("name", FIELD_TYPE_STRING)])
    layer.updateFields()

    from qgis.core import QgsPointXY

    to_map, _ = local_frame_transforms(origin_map_xy, crs_authid)
    features = []
    for chain_name, xyz in chains:
        pts_arr = np.asarray(xyz, dtype=float)
        if pts_arr.ndim != 2 or pts_arr.shape[1] < 3 or len(pts_arr) < 2:
            raise ValueError(
                f"Chain {chain_name!r} must be an (n, 3) array with n >= 2."
            )
        points = []
        for x, y, z in pts_arr[:, :3]:
            mp = to_map.transform(QgsPointXY(float(x), float(y)))
            points.append(QgsPoint(mp.x(), mp.y(), float(z)))
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry(QgsLineString(points)))
        feature["name"] = str(chain_name)
        features.append(feature)

    if features and not provider.addFeatures(features):
        raise RuntimeError(f"Failed to add features to memory layer {name!r}.")
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def push_markers_to_map(
    name: str,
    markers: Sequence[Tuple[str, Tuple[float, float, float]]],
    origin_map_xy: Tuple[float, float],
    crs_authid: str,
):
    """Add local-frame labelled points to the map as a PointZ layer.

    ``markers`` is (label, (x, y, z) local) tuples. Returns the layer.
    """
    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsPoint,
        QgsProject,
        QgsVectorLayer,
    )
    from ....qgis_compat import FIELD_TYPE_STRING

    layer = QgsVectorLayer(f"PointZ?crs={crs_authid}", name, "memory")
    if not layer.isValid():
        raise RuntimeError(
            f"Could not create memory layer {name!r} (crs={crs_authid!r})."
        )
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("label", FIELD_TYPE_STRING)])
    layer.updateFields()

    from qgis.core import QgsPointXY

    to_map, _ = local_frame_transforms(origin_map_xy, crs_authid)
    features = []
    for label, xyz in markers:
        x, y, z = (float(v) for v in xyz)
        mp = to_map.transform(QgsPointXY(x, y))
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry(QgsPoint(mp.x(), mp.y(), z)))
        feature["label"] = str(label)
        features.append(feature)

    if features and not provider.addFeatures(features):
        raise RuntimeError(f"Failed to add features to memory layer {name!r}.")
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer
