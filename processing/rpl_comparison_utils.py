# -*- coding: utf-8 -*-
"""
RPL Comparison Utilities
Provides shared, reusable functions for comparing and translating KP values between different RPL (Route Position List) layers.

This module contains the core logic for:
- Calculating KP (chainage) along a line layer
- Translating KP values from one RPL to another via spatial proximity
- Computing Distance Cross Course (DCC) between points and lines
- Cross-referencing features between RPLs

Usage:
    from processing.rpl_comparison_utils import RPLComparator

    comparator = RPLComparator(source_line, target_line, crs, context)
    translation = comparator.translate_kp(source_kp_km)
    # Returns: {'target_kp': 49.8, 'spatial_offset_m': 0.32, 'target_point': QgsPoint}

Implementation note (1.6):
    ``RPLComparator`` is now a thin facade over :class:`kp_geo_utils.RouteFrame`
    and :func:`kp_range_utils.make_distance_area`. The public method signatures
    and attribute names are unchanged. The previous inline distance-area setup
    silently fell back to planar measurements when the project ellipsoid was
    unset; the new path applies the same WGS84 fallback that 1.5.1 introduced
    elsewhere.
"""

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
)
from ..qgis_compat import GEOMETRY_POINT

from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area


class RPLComparator:
    """
    Handles accurate comparison and translation between two RPL line layers.

    Provides methods to:
    - Calculate KP (chainage) along a line
    - Translate KP values from source RPL to target RPL using spatial proximity
    - Compute Distance Cross Course (DCC) - perpendicular distance from a point to a line
    - Cross-reference point features between RPLs
    """

    def __init__(self, source_line_layer, target_line_layer, crs, context):
        """
        Initialize RPL comparator with two line layers.

        Args:
            source_line_layer: QgsVectorLayer (line geometry) - source RPL
            target_line_layer: QgsVectorLayer (line geometry) - target RPL
            crs: QgsCoordinateReferenceSystem - shared CRS for both layers
            context: QgsProcessingContext - for coordinate transformations and ellipsoid info
        """
        self.source_line_layer = source_line_layer
        self.target_line_layer = target_line_layer
        self.crs = crs
        self.context = context

        project = context.project() if context is not None else None
        transform_context = (
            context.transformContext() if context is not None else None
        )
        # Always ellipsoidal here — the previous implementation effectively
        # required this (and silently broke when the ellipsoid was unset).
        self.distance_calculator = make_distance_area(
            crs, transform_context, mode="ellipsoidal", project=project
        )

        # Build cached route frames over each layer. RouteFrame iterates the
        # provider once and caches geometries + cumulative offsets.
        self._source_frame = RouteFrame.from_source(
            source_line_layer, self.distance_calculator
        )
        self._target_frame = RouteFrame.from_source(
            target_line_layer, self.distance_calculator
        )

        # Back-compat attributes (callers in rpl_route_comparison_algorithm
        # iterate ``comparator.source_geoms`` directly).
        self.source_geoms = self._source_frame.geometries
        self.target_geoms = self._target_frame.geometries
        # Per-feature lengths (metres), preserved from the pre-1.6 attribute shape.
        self.source_segment_lengths = [
            self.distance_calculator.measureLength(g) for g in self.source_geoms
        ]
        self.target_segment_lengths = [
            self.distance_calculator.measureLength(g) for g in self.target_geoms
        ]
        self.total_source_length_m = self._source_frame.total_length_m
        self.total_target_length_m = self._target_frame.total_length_m

    # ------------------------------------------------------------------ helpers

    def _frame(self, source: bool) -> RouteFrame:
        return self._source_frame if source else self._target_frame

    # ------------------------------------------------------------------ public

    def get_point_at_kp(self, kp_km, source=True):
        """
        Get the geographic point at a specific KP on a line.

        Args:
            kp_km: KP value in kilometers
            source: If True, use source line; if False, use target line

        Returns:
            QgsPointXY at the given KP, or None if KP is out of range
        """
        return self._frame(source).point_at_kp(kp_km)

    def calculate_kp_to_point(self, point_xy, source=True):
        """
        Calculate the KP (chainage) to a specific point on a line.
        Finds the nearest point on the line and returns its KP value.

        Args:
            point_xy: QgsPointXY - the point to calculate KP for
            source: If True, calculate on source line; if False, on target line

        Returns:
            KP value in kilometers
        """
        if point_xy is None:
            return 0.0
        hit = self._frame(source).kp_at_point(QgsPointXY(point_xy))
        return hit.kp_km

    def nearest_point_on_line(self, point_xy, source=True):
        """
        Find the nearest point on a line to a given point.

        Args:
            point_xy: QgsPointXY - reference point
            source: If True, search on source line; if False, on target line

        Returns:
            {'point': QgsPointXY, 'distance': float (meters)}
        """
        if point_xy is None:
            return {"point": None, "distance": float("inf")}
        hit = self._frame(source).kp_at_point(QgsPointXY(point_xy))
        return {"point": hit.snapped_xy, "distance": hit.dcc_m}

    def distance_cross_course(self, point_xy, source=True):
        """
        Calculate Distance Cross Course (DCC) - perpendicular distance from a point to a line.
        This is the shortest distance from the point to the line.

        Args:
            point_xy: QgsPointXY - the point
            source: If True, measure to source line; if False, to target line

        Returns:
            Distance in meters
        """
        if point_xy is None:
            return float("inf")
        hit = self._frame(source).kp_at_point(QgsPointXY(point_xy))
        return hit.dcc_m

    def translate_kp(self, source_kp_km):
        """
        Translate a KP value from source RPL to target RPL.

        Finds the geographic point on source line at the given KP,
        then finds the nearest point on target line and returns its KP.

        Args:
            source_kp_km: KP value (in km) on source line

        Returns:
            {
                'source_kp': float (km),
                'target_kp': float (km),
                'spatial_offset_m': float (perpendicular distance from source point to target line),
                'target_point': QgsPointXY,
                'source_point': QgsPointXY
            }
        """
        source_point = self.get_point_at_kp(source_kp_km, source=True)
        if source_point is None:
            return None

        hit = self._target_frame.kp_at_point(source_point)
        if hit.snapped_xy is None:
            return None

        return {
            "source_kp": source_kp_km,
            "target_kp": hit.kp_km,
            "spatial_offset_m": hit.dcc_m,
            "target_point": hit.snapped_xy,
            "source_point": source_point,
        }

    def cross_reference_point_features(self, source_point_layer):
        """
        Cross-reference point features from source point layer to target line.
        For each point feature in source, find its corresponding location on target line.

        Args:
            source_point_layer: QgsVectorLayer (point geometry) - e.g., Design RPL events

        Returns:
            List of dictionaries:
            {
                'feature_id': int,
                'source_kp': float (km) - if DistCumulative field exists,
                'target_kp': float (km),
                'spatial_offset_m': float,
                'geometry_point': QgsPointXY,
                'attributes': dict (original point attributes)
            }
        """
        results = []
        dist_cumul_idx = source_point_layer.fields().lookupField("DistCumulative")

        for feature in source_point_layer.getFeatures():
            point_geom = feature.geometry()
            if point_geom.isEmpty() or point_geom.type() != GEOMETRY_POINT:
                continue

            point_xy = point_geom.asPoint()
            hit = self._target_frame.kp_at_point(QgsPointXY(point_xy))
            if hit.snapped_xy is None:
                continue

            source_kp = None
            if dist_cumul_idx >= 0:
                source_kp = feature[dist_cumul_idx]

            results.append(
                {
                    "feature_id": feature.id(),
                    "source_kp": source_kp,
                    "target_kp": hit.kp_km,
                    "spatial_offset_m": hit.dcc_m,
                    "geometry_point": hit.snapped_xy,
                    "attributes": feature.attributes(),
                }
            )

        return results

    def translate_kp_for_point(self, point_xy, dist_cumul_idx, source_feature):
        """
        Translate a point's KP from source RPL to target RPL.
        This is the main method used by the processing algorithm.

        Args:
            point_xy: QgsPointXY - the point location
            dist_cumul_idx: Field index for DistCumulative in source feature (-1 if not present)
            source_feature: QgsFeature - the source point feature

        Returns:
            {
                'source_kp': float (km) - KP on source line (if field exists, else None),
                'translated_kp': float (km) - KP on target line,
                'spatial_offset_m': float - distance from point to target line,
                'dcc_to_source_line': float - perpendicular distance to source line,
                'target_point': QgsPointXY - the nearest point on target line
            }
        """
        source_kp = None
        if dist_cumul_idx >= 0:
            source_kp = source_feature[dist_cumul_idx]

        target_hit = self._target_frame.kp_at_point(QgsPointXY(point_xy))
        if target_hit.snapped_xy is None:
            return None

        source_hit = self._source_frame.kp_at_point(target_hit.snapped_xy)

        return {
            "source_kp": source_kp,
            "translated_kp": target_hit.kp_km,
            "spatial_offset_m": target_hit.dcc_m,
            "dcc_to_source_line": source_hit.dcc_m,
            "target_point": target_hit.snapped_xy,
        }
