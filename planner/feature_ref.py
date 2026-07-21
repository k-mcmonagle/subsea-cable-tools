# -*- coding: utf-8 -*-
"""Resolve persisted task feature references and cache measured route frames."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Dict, Optional, Tuple

from qgis.core import QgsFeatureRequest, QgsProject, QgsWkbTypes

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area
from ..qgis_compat import GEOMETRY_LINE, GEOMETRY_POINT


@dataclass
class ResolvedFeature:
    layer: object
    feature: object
    geom_kind: str


class FeatureReferenceResolver:
    def __init__(self, project=None, canvas=None, store=None):
        self.project = project or QgsProject.instance()
        self.canvas = canvas
        self.store = store
        self._route_cache: Dict[Tuple[str, str, str], RouteFrame] = {}

    def clear_cache(self):
        self._route_cache.clear()

    def resolve(self, ref) -> Optional[ResolvedFeature]:
        if self.store is not None and _is_owned(ref):
            owned = self.store.get_task_geometry(_get(ref, "task_id"))
            if owned is not None:
                layer, feature, kind = owned
                if not feature.hasGeometry() or feature.geometry().isEmpty():
                    return None
                return ResolvedFeature(layer, feature, kind)
        layer_id = _get(ref, "layer_id")
        layer = self.project.mapLayer(layer_id) if layer_id else None
        if layer is None:
            source = _get(ref, "layer_source")
            for candidate in self.project.mapLayers().values():
                try:
                    if source and candidate.source() == source:
                        layer = candidate
                        break
                except Exception:
                    continue
        if layer is None:
            return None
        feature_id = _get(ref, "feature_id")
        try:
            request = QgsFeatureRequest().setFilterFid(int(feature_id))
            feature = next(layer.getFeatures(request), None)
        except (TypeError, ValueError):
            feature = None
        if feature is None or not feature.isValid() or not feature.hasGeometry():
            return None
        geometry_type = QgsWkbTypes.geometryType(feature.geometry().wkbType())
        geom_kind = "line" if geometry_type == GEOMETRY_LINE else (
            "point" if geometry_type == GEOMETRY_POINT else ""
        )
        if not geom_kind:
            return None
        return ResolvedFeature(layer, feature, geom_kind)

    def route_frame(self, ref) -> Optional[RouteFrame]:
        resolved = self.resolve(ref)
        if resolved is None or resolved.geom_kind != "line":
            return None
        target_crs = self.canvas.mapSettings().destinationCrs() if self.canvas else resolved.layer.crs()
        crs_key = target_crs.authid() or target_crs.toWkt()
        key = (resolved.layer.id(), str(resolved.feature.id()), crs_key)
        cached = self._route_cache.get(key)
        if cached is not None:
            return cached
        distance = make_distance_area(target_crs, self.project.transformContext(), project=self.project)
        try:
            frame = RouteFrame.from_source(
                [resolved.feature.geometry()], distance, target_crs=target_crs,
                source_crs=resolved.layer.crs(), project=self.project,
                follow_stored_geometry=True,
            )
        except Exception:
            # Degenerate/edited geometry must degrade to "unmeasured", not
            # raise out of a table-recompute slot.
            return None
        self._route_cache[key] = frame
        return frame

    def route_length_m(self, ref) -> Optional[float]:
        frame = self.route_frame(ref)
        return frame.total_length_m if frame is not None else None

    def point_at_chainage(self, ref, chainage_m: float):
        resolved = self.resolve(ref)
        if resolved is None:
            return None
        if resolved.geom_kind == "point":
            try:
                point = resolved.feature.geometry().asPoint()
            except Exception:
                return None
            if self.canvas and resolved.layer.crs() != self.canvas.mapSettings().destinationCrs():
                from qgis.core import QgsCoordinateTransform, QgsPointXY
                transform = QgsCoordinateTransform(
                    resolved.layer.crs(), self.canvas.mapSettings().destinationCrs(), self.project)
                return transform.transform(QgsPointXY(point))
            return point
        frame = self.route_frame(ref)
        return frame.point_at_kp(float(chainage_m or 0.0) / 1000.0, clamp=True) if frame else None

    def location_point(self, ref):
        """Resolve a point feature or an explicit position on a linked line."""
        resolved = self.resolve(ref)
        if resolved is None:
            return None
        if resolved.geom_kind == "point":
            return self.point_at_chainage(ref, 0.0)
        frame = self.route_frame(ref)
        if frame is None:
            return None
        mode = _get(ref, "location_mode", "feature") or "feature"
        if mode == "line_start":
            chainage_m = 0.0
        elif mode == "line_end":
            chainage_m = frame.total_length_m
        elif mode == "route_chainage":
            try:
                chainage_m = float(_get(ref, "location_chainage_m", 0.0) or 0.0)
            except (TypeError, ValueError):
                chainage_m = 0.0
        else:
            return None
        return frame.point_at_kp(chainage_m / 1000.0, clamp=True)


def feature_reference(layer, feature, label="") -> Dict:
    geometry_type = QgsWkbTypes.geometryType(feature.geometry().wkbType())
    kind = "line" if geometry_type == GEOMETRY_LINE else (
        "point" if geometry_type == GEOMETRY_POINT else ""
    )
    return {
        "layer_id": layer.id(), "layer_source": layer.source(), "layer_name": layer.name(),
        "feature_id": str(feature.id()), "feature_label": str(label or feature.id()),
        "geom_kind": kind,
    }


def _get(obj, key, default=""):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _is_owned(ref) -> bool:
    raw = _get(ref, "linked_ref_json")
    if not raw:
        return False
    try:
        return bool(json.loads(str(raw)).get("owned_geometry"))
    except (TypeError, ValueError):
        return False
