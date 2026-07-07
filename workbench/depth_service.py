# -*- coding: utf-8 -*-
"""DepthService — depth sampling for the workbench, outside processing.

Wraps the shared samplers in processing/depth_sampling.py behind a config
dict (stored as JSON in wb_rpl.depth_source_config):

{
  "mode": 0,                      # 0 Auto, 1 Raster only, 2 Contours only
  "raster_layer_ids": ["..."],
  "raster_band": 1,
  "contour_layers": [{"layer_id": "...", "depth_field": "depth"}, ...],
  "contour_search_radius_m": 500.0,
  "auto_resample": true
}

Query points are WGS84 (lon/lat) to match the workbench layers.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from ..processing import depth_sampling

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")


class DepthSourceConfig:
    def __init__(self, data: Optional[Dict] = None):
        data = data or {}
        self.mode = int(data.get("mode", 0))
        self.raster_layer_ids: List[str] = list(data.get("raster_layer_ids", []))
        self.raster_band = int(data.get("raster_band", 1))
        self.contour_layers: List[Dict] = list(data.get("contour_layers", []))
        self.contour_search_radius_m = float(data.get("contour_search_radius_m", 0.0))
        self.auto_resample = bool(data.get("auto_resample", True))

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode,
            "raster_layer_ids": self.raster_layer_ids,
            "raster_band": self.raster_band,
            "contour_layers": self.contour_layers,
            "contour_search_radius_m": self.contour_search_radius_m,
            "auto_resample": self.auto_resample,
        }

    def is_configured(self) -> bool:
        return bool(self.raster_layer_ids or self.contour_layers)


class DepthService:
    def __init__(self, config: DepthSourceConfig, project: Optional[QgsProject] = None):
        self.config = config
        self.project = project or QgsProject.instance()
        rasters = [
            layer for layer in (self.project.mapLayer(i) for i in config.raster_layer_ids)
            if isinstance(layer, QgsRasterLayer)
        ]
        contour_layers = []
        depth_fields = []
        for entry in config.contour_layers:
            layer = self.project.mapLayer(entry.get("layer_id", ""))
            if isinstance(layer, QgsVectorLayer):
                contour_layers.append(layer)
                depth_fields.append(entry.get("depth_field", ""))
        self._raster_samplers = depth_sampling.build_raster_samplers(rasters, WGS84)
        self._contour_samplers = depth_sampling.build_contour_samplers(
            contour_layers, depth_fields, WGS84
        )

    def is_available(self) -> bool:
        return bool(self._raster_samplers or self._contour_samplers)

    def sample(self, lat: float, lon: float) -> Optional[float]:
        if not self.is_available():
            return None
        return depth_sampling.sample_depth(
            QgsPointXY(lon, lat),
            self.config.mode,
            self._raster_samplers,
            self._contour_samplers,
            self.config.contour_search_radius_m,
            self.project.transformContext(),
            project=self.project,
            band=self.config.raster_band,
        )

    def sample_many(self, coords: Sequence[Tuple[float, float]]) -> List[Optional[float]]:
        return [self.sample(lat, lon) for lat, lon in coords]

    def sample_profile(self, route_frame, kp0_km: float, kp1_km: float, step_m: float = 25.0
                       ) -> List[Tuple[float, float]]:
        """(kp_km, depth_m) pairs along a RouteFrame between two KPs."""
        out: List[Tuple[float, float]] = []
        if step_m <= 0:
            step_m = 25.0
        kp = min(kp0_km, kp1_km)
        end = max(kp0_km, kp1_km)
        step_km = step_m / 1000.0
        while kp <= end + 1e-9:
            point = route_frame.point_at_kp(min(kp, end), clamp=True)
            if point is not None:
                depth = self.sample(point.y(), point.x())
                if depth is not None:
                    out.append((min(kp, end), float(depth)))
            kp += step_km
        return out
