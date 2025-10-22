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
"""

from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsDistanceArea,
    QgsProject,
    QgsWkbTypes,
    QgsPoint
)


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
        
        # Cache source and target line geometries
        self.source_geoms = []  # List of QgsGeometry objects
        self.source_segment_lengths = []  # Cumulative lengths for each source geometry
        self.total_source_length_m = 0.0
        
        self.target_geoms = []  # List of QgsGeometry objects
        self.target_segment_lengths = []  # Cumulative lengths for each target geometry
        self.total_target_length_m = 0.0
        
        # Initialize distance calculator
        self.distance_calculator = QgsDistanceArea()
        self.distance_calculator.setSourceCrs(crs, context.transformContext())
        ellipsoid = context.project().ellipsoid() if context.project() else 'WGS84'
        if ellipsoid:
            self.distance_calculator.setEllipsoid(ellipsoid)
        
        # Load geometries from both layers
        self._load_geometries()
    
    def _load_geometries(self):
        """Load and cache all line geometries from source and target layers."""
        # Load source geometries
        self.source_geoms = []
        self.source_segment_lengths = []
        self.total_source_length_m = 0.0
        
        for feature in self.source_line_layer.getFeatures():
            geom = QgsGeometry(feature.geometry())
            self.source_geoms.append(geom)
            segment_length = self.distance_calculator.measureLength(geom)
            self.source_segment_lengths.append(segment_length)
            self.total_source_length_m += segment_length
        
        # Load target geometries
        self.target_geoms = []
        self.target_segment_lengths = []
        self.total_target_length_m = 0.0
        
        for feature in self.target_line_layer.getFeatures():
            geom = QgsGeometry(feature.geometry())
            self.target_geoms.append(geom)
            segment_length = self.distance_calculator.measureLength(geom)
            self.target_segment_lengths.append(segment_length)
            self.total_target_length_m += segment_length
    
    def get_point_at_kp(self, kp_km, source=True):
        """
        Get the geographic point at a specific KP on a line.
        
        Args:
            kp_km: KP value in kilometers
            source: If True, use source line; if False, use target line
        
        Returns:
            QgsPointXY at the given KP, or None if KP is out of range
        """
        geoms = self.source_geoms if source else self.target_geoms
        if not geoms:
            return None
        
        kp_m = kp_km * 1000.0
        cumulative_length = 0.0
        
        for geom in geoms:
            # Get all vertices of this geometry
            if geom.isMultipart():
                parts = geom.asMultiPolyline()
            else:
                parts = [geom.asPolyline()]
            
            for part in parts:
                for i in range(len(part) - 1):
                    vertex1 = QgsPointXY(part[i])
                    vertex2 = QgsPointXY(part[i + 1])
                    segment_length = self.distance_calculator.measureLine(vertex1, vertex2)
                    
                    if cumulative_length + segment_length >= kp_m:
                        # KP is within this segment
                        ratio = (kp_m - cumulative_length) / segment_length if segment_length > 0 else 0
                        interpolated_point = QgsPointXY(
                            vertex1.x() + ratio * (vertex2.x() - vertex1.x()),
                            vertex1.y() + ratio * (vertex2.y() - vertex1.y())
                        )
                        return interpolated_point
                    
                    cumulative_length += segment_length
        
        return None
    
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
        geoms = self.source_geoms if source else self.target_geoms
        if not geoms:
            return 0.0
        
        point_geom = QgsGeometry.fromPointXY(point_xy)
        cumulative_length = 0.0
        min_dist = float('inf')
        nearest_kp_m = 0.0
        
        for geom in geoms:
            # Find nearest point on this geometry
            nearest_geom = geom.nearestPoint(point_geom)
            if nearest_geom.isEmpty():
                continue
            
            nearest_pt = nearest_geom.asPoint()
            dist = self.distance_calculator.measureLine(
                point_xy,
                QgsPointXY(nearest_pt)
            )
            
            if dist < min_dist:
                min_dist = dist
                
                # Calculate KP to this nearest point
                temp_kp_m = self._calculate_kp_to_point_on_geom(
                    nearest_pt,
                    geom,
                    cumulative_length
                )
                nearest_kp_m = temp_kp_m
            
            cumulative_length += self.distance_calculator.measureLength(geom)
        
        return nearest_kp_m / 1000.0  # Convert to km
    
    def _calculate_kp_to_point_on_geom(self, target_point, geom, cumulative_base):
        """
        Helper: Calculate KP to a point known to be on/near a specific geometry.
        
        Args:
            target_point: QgsPoint - the point (assumed to be on/near geom)
            geom: QgsGeometry - the geometry to measure along
            cumulative_base: Base KP (meters) from previous geometries
        
        Returns:
            KP in meters (including cumulative_base)
        """
        target_pt_xy = QgsPointXY(target_point)
        cumulative_length = cumulative_base
        
        if geom.isMultipart():
            parts = geom.asMultiPolyline()
        else:
            parts = [geom.asPolyline()]
        
        for part in parts:
            for i in range(len(part) - 1):
                vertex1 = QgsPointXY(part[i])
                vertex2 = QgsPointXY(part[i + 1])
                segment_length = self.distance_calculator.measureLine(vertex1, vertex2)
                
                # Create segment geometry
                segment_geom = QgsGeometry.fromPolylineXY([vertex1, vertex2])
                
                # Find nearest point on this segment
                nearest_on_segment = segment_geom.nearestPoint(
                    QgsGeometry.fromPointXY(target_pt_xy)
                )
                if not nearest_on_segment.isEmpty():
                    nearest_pt = nearest_on_segment.asPoint()
                    dist_to_nearest = self.distance_calculator.measureLine(
                        target_pt_xy,
                        QgsPointXY(nearest_pt)
                    )
                    
                    # If this segment contains our target point, interpolate
                    if dist_to_nearest < 0.1:  # Within 10 cm
                        dist_along_segment = self.distance_calculator.measureLine(
                            vertex1,
                            QgsPointXY(nearest_pt)
                        )
                        return cumulative_length + dist_along_segment
                
                cumulative_length += segment_length
        
        return cumulative_base
    
    def nearest_point_on_line(self, point_xy, source=True):
        """
        Find the nearest point on a line to a given point.
        
        Args:
            point_xy: QgsPointXY - reference point
            source: If True, search on source line; if False, on target line
        
        Returns:
            {'point': QgsPointXY, 'distance': float (meters)}
        """
        geoms = self.source_geoms if source else self.target_geoms
        if not geoms:
            return {'point': None, 'distance': float('inf')}
        
        point_geom = QgsGeometry.fromPointXY(point_xy)
        min_distance = float('inf')
        nearest_point = None
        
        for geom in geoms:
            nearest_geom = geom.nearestPoint(point_geom)
            if nearest_geom.isEmpty():
                continue
            
            nearest_pt = nearest_geom.asPoint()
            distance = self.distance_calculator.measureLine(
                point_xy,
                QgsPointXY(nearest_pt)
            )
            
            if distance < min_distance:
                min_distance = distance
                nearest_point = QgsPointXY(nearest_pt)
        
        return {
            'point': nearest_point,
            'distance': min_distance
        }
    
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
        # Step 1: Get point on source line at source_kp_km
        source_point = self.get_point_at_kp(source_kp_km, source=True)
        if source_point is None:
            return None
        
        # Step 2: Find nearest point on target line
        nearest_result = self.nearest_point_on_line(source_point, source=False)
        if nearest_result['point'] is None:
            return None
        
        target_point = nearest_result['point']
        spatial_offset = nearest_result['distance']
        
        # Step 3: Calculate KP on target line
        target_kp_km = self.calculate_kp_to_point(target_point, source=False)
        
        return {
            'source_kp': source_kp_km,
            'target_kp': target_kp_km,
            'spatial_offset_m': spatial_offset,
            'target_point': target_point,
            'source_point': source_point
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
        
        # Check if DistCumulative field exists in source point layer
        dist_cumul_idx = source_point_layer.fields().lookupField('DistCumulative')
        
        for feature in source_point_layer.getFeatures():
            point_geom = feature.geometry()
            if point_geom.isEmpty() or point_geom.type() != QgsWkbTypes.PointGeometry:
                continue
            
            point_xy = point_geom.asPoint()
            
            # Find nearest on target
            nearest_result = self.nearest_point_on_line(point_xy, source=False)
            if nearest_result['point'] is None:
                continue
            
            target_point = nearest_result['point']
            spatial_offset = nearest_result['distance']
            
            # Calculate target KP
            target_kp = self.calculate_kp_to_point(target_point, source=False)
            
            # Get source KP if available
            source_kp = None
            if dist_cumul_idx >= 0:
                source_kp = feature[dist_cumul_idx]
            
            result = {
                'feature_id': feature.id(),
                'source_kp': source_kp,
                'target_kp': target_kp,
                'spatial_offset_m': spatial_offset,
                'geometry_point': target_point,
                'attributes': feature.attributes()
            }
            results.append(result)
        
        return results
    
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
        result = self.nearest_point_on_line(point_xy, source=source)
        return result['distance']
    
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
        # Get source KP if DistCumulative field exists
        source_kp = None
        if dist_cumul_idx >= 0:
            source_kp = source_feature[dist_cumul_idx]
        
        # Find nearest on target line
        nearest_result = self.nearest_point_on_line(point_xy, source=False)
        if nearest_result['point'] is None:
            return None
        
        target_point = nearest_result['point']
        spatial_offset = nearest_result['distance']
        
        # Calculate target KP
        target_kp = self.calculate_kp_to_point(target_point, source=False)
        
        # Calculate DCC to source line
        dcc_to_source = self.distance_cross_course(target_point, source=True)
        
        return {
            'source_kp': source_kp,
            'translated_kp': target_kp,
            'spatial_offset_m': spatial_offset,
            'dcc_to_source_line': dcc_to_source,
            'target_point': target_point
        }
