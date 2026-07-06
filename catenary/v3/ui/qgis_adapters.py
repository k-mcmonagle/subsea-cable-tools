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

    Sampling points are laid out on a regular grid in the **project** map
    CRS (which must be projected/metric) spanning ``+/- half_extent_m``
    about ``center_map_xy``, then transformed to the raster layer's CRS
    for the identify calls.

    Returns a dict with keys ``x0, y0, dx, dy, depths`` (2D list, positive
    down; row ``i`` at ``y0 + i*dy``, column ``j`` at ``x0 + j*dx``) in
    LOCAL coordinates (centre = local ``(0, 0)``, so ``x0 = -half_extent``),
    plus ``origin_map_xy`` (the centre) and ``crs_authid`` (project CRS).
    The dict feeds ``GridBathymetry(x0, y0, dx, dy, depths)`` directly.

    If ``depths_positive_down`` is False the raster is treated as holding
    negative-down elevations and values are negated. Nodata / failed
    samples are filled with the mean of the finite values.
    """
    from qgis.core import (
        QgsCoordinateTransform,
        QgsPointXY,
        QgsProject,
        QgsRaster,
        QgsRasterLayer,
    )

    if n < 2:
        raise ValueError("n must be at least 2 for a usable grid.")
    if not (float(half_extent_m) > 0.0):
        raise ValueError("half_extent_m must be positive.")

    project = QgsProject.instance()
    project_crs = project.crs()
    if not project_crs.isValid() or project_crs.isGeographic():
        raise ValueError(
            "project CRS must be projected (metres) for bathymetry sampling"
        )

    layer = project.mapLayer(layer_id)
    if layer is None:
        raise ValueError(f"No layer with id {layer_id!r} in the project.")
    if not isinstance(layer, QgsRasterLayer):
        raise ValueError(f"Layer {layer.name()!r} is not a raster layer.")
    provider = layer.dataProvider()
    if provider is None:
        raise RuntimeError(f"Raster layer {layer.name()!r} has no data provider.")

    try:
        transform = QgsCoordinateTransform(
            project_crs, layer.crs(), project.transformContext()
        )
    except Exception as exc:  # noqa: BLE001 - re-raise with context
        raise RuntimeError(
            f"Could not build CRS transform {project_crs.authid()} -> "
            f"{layer.crs().authid()}: {exc}"
        ) from exc

    cx, cy = float(center_map_xy[0]), float(center_map_xy[1])
    half = float(half_extent_m)
    n = int(n)
    step = 2.0 * half / (n - 1)
    xs_local = np.linspace(-half, half, n)
    ys_local = np.linspace(-half, half, n)

    depths = np.full((n, n), np.nan, dtype=float)
    for i, yl in enumerate(ys_local):
        for j, xl in enumerate(xs_local):
            point = QgsPointXY(cx + float(xl), cy + float(yl))
            try:
                point = transform.transform(point)
            except Exception:
                continue  # outside transform validity -> leave as nodata
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

    finite = depths[np.isfinite(depths)]
    if finite.size == 0:
        raise ValueError(
            f"No valid raster values sampled from {layer.name()!r} around "
            f"({cx:.1f}, {cy:.1f}) +/- {half:.0f} m — check the layer extent, "
            "band and CRS."
        )
    depths = np.where(np.isfinite(depths), depths, float(finite.mean()))

    return {
        "x0": -half,
        "y0": -half,
        "dx": step,
        "dy": step,
        "depths": depths.tolist(),
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

    ``chains`` is (chain name, (n, 3) local xyz) tuples; vertices become
    ``(x + ox, y + oy, z)`` in the CRS ``crs_authid``. Returns the layer.
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
    from qgis.PyQt.QtCore import QVariant

    layer = QgsVectorLayer(f"LineStringZ?crs={crs_authid}", name, "memory")
    if not layer.isValid():
        raise RuntimeError(
            f"Could not create memory layer {name!r} (crs={crs_authid!r})."
        )
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("name", QVariant.String)])
    layer.updateFields()

    ox, oy = float(origin_map_xy[0]), float(origin_map_xy[1])
    features = []
    for chain_name, xyz in chains:
        pts_arr = np.asarray(xyz, dtype=float)
        if pts_arr.ndim != 2 or pts_arr.shape[1] < 3 or len(pts_arr) < 2:
            raise ValueError(
                f"Chain {chain_name!r} must be an (n, 3) array with n >= 2."
            )
        points = [
            QgsPoint(float(x) + ox, float(y) + oy, float(z))
            for x, y, z in pts_arr[:, :3]
        ]
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
    from qgis.PyQt.QtCore import QVariant

    layer = QgsVectorLayer(f"PointZ?crs={crs_authid}", name, "memory")
    if not layer.isValid():
        raise RuntimeError(
            f"Could not create memory layer {name!r} (crs={crs_authid!r})."
        )
    provider = layer.dataProvider()
    provider.addAttributes([QgsField("label", QVariant.String)])
    layer.updateFields()

    ox, oy = float(origin_map_xy[0]), float(origin_map_xy[1])
    features = []
    for label, xyz in markers:
        x, y, z = (float(v) for v in xyz)
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry(QgsPoint(x + ox, y + oy, z)))
        feature["label"] = str(label)
        features.append(feature)

    if features and not provider.addFeatures(features):
        raise RuntimeError(f"Failed to add features to memory layer {name!r}.")
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer
