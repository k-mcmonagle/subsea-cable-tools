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
        # Lazy import avoids a module cycle: the snapshot consumes the
        # configuration class above, while this service uses its sampler.
        from ..burial.analysis_task import DepthSnapshot
        self._snapshot = DepthSnapshot(config, self.project)

    def is_available(self):
        return self._snapshot.is_available()

    def sample(self, lat, lon):
        return self._snapshot.sample(lat, lon)


    def sample_many(self, coords: Sequence[Tuple[float, float]]) -> List[Optional[float]]:
        return [self.sample(lat, lon) for lat, lon in coords]

    def sample_profile(self, route_frame, kp0_km: float, kp1_km: float, step_m: float = 25.0
                       ) -> List[Tuple[float, float]]:
        """(kp_km, depth_m) pairs along a RouteFrame between two KPs."""
        import math
        lo, hi = sorted([kp0_km, kp1_km])
        step = max(step_m, 1) / 1000
        marks = [min(hi, lo+i*step) for i in range(int(math.ceil((hi-lo)/step))+1)]
        self._snapshot.prepare()
        return self._snapshot.profile_samples(route_frame, marks)
