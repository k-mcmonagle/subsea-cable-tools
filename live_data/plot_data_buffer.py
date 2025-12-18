"""
Plot Data Buffer

Efficient circular buffer for managing time-series data.
Handles timestamped data points with automatic expiration based on time window.
"""

from collections import deque
from typing import List, Tuple, Optional, Dict, Any
from datetime import datetime
import statistics


class PlotDataBuffer:
    """
    Circular buffer for storing time-series data points.
    
    Automatically expires points outside the configured time window.
    Provides efficient statistics calculation (mean, min, max, std dev).
    """
    
    def __init__(self, max_points: int = 1000, time_window: int = 300):
        """
        Initialize the data buffer.
        
        Args:
            max_points: Maximum number of points to keep (memory limit)
            time_window: Duration in seconds for valid data (e.g., 300 = 5 minutes)
        """
        self.max_points = max_points
        self.time_window = time_window
        self.points: deque = deque(maxlen=max_points)  # Stores (timestamp, value) tuples
        self.last_timestamp: Optional[float] = None
        
    def add_value(self, timestamp: float, value: Optional[float]) -> None:
        """
        Add a data point to the buffer.
        
        Args:
            timestamp: Unix timestamp of the data point
            value: Numeric value, or None for missing data
        """
        if value is None:
            return  # Skip None values
            
        try:
            # Convert to float to ensure numeric type
            value = float(value)
        except (ValueError, TypeError):
            return  # Skip non-numeric values
        
        self.points.append((timestamp, value))
        self.last_timestamp = timestamp
        
        # Automatically expire old points
        self.remove_old_points(timestamp - self.time_window)
    
    def remove_old_points(self, before_timestamp: float) -> None:
        """
        Remove all points with timestamp before the given time.
        
        Args:
            before_timestamp: Remove points older than this timestamp
        """
        while self.points and self.points[0][0] < before_timestamp:
            self.points.popleft()
    
    def get_points(self) -> List[Tuple[float, float]]:
        """
        Get all data points in the buffer.
        
        Returns:
            List of (timestamp, value) tuples
        """
        return list(self.points)
    
    def get_time_range(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Get the time range of current data.
        
        Returns:
            Tuple of (min_timestamp, max_timestamp), or (None, None) if empty
        """
        if not self.points:
            return None, None
        
        timestamps = [t for t, _ in self.points]
        return min(timestamps), max(timestamps)
    
    def get_value_range(self) -> Tuple[Optional[float], Optional[float]]:
        """
        Get the min and max values in the buffer.
        
        Returns:
            Tuple of (min_value, max_value), or (None, None) if empty
        """
        if not self.points:
            return None, None
        
        values = [v for _, v in self.points]
        return min(values), max(values)
    
    def get_statistics(self) -> Dict[str, float]:
        """
        Calculate statistics for the buffered data.
        
        Returns:
            Dictionary with keys: 'count', 'mean', 'min', 'max', 'std_dev'
        """
        if not self.points:
            return {
                'count': 0,
                'mean': 0.0,
                'min': 0.0,
                'max': 0.0,
                'std_dev': 0.0,
            }
        
        values = [v for _, v in self.points]
        
        mean_val = statistics.mean(values)
        min_val = min(values)
        max_val = max(values)
        std_dev = statistics.stdev(values) if len(values) > 1 else 0.0
        
        return {
            'count': len(values),
            'mean': mean_val,
            'min': min_val,
            'max': max_val,
            'std_dev': std_dev,
        }
    
    def get_rolling_average(self, window_size: int) -> List[Tuple[float, float]]:
        """
        Calculate rolling average over a time window.
        
        Args:
            window_size: Window size in seconds
        
        Returns:
            List of (timestamp, average_value) tuples
        """
        if not self.points or window_size <= 0:
            return []
        
        result = []
        points_list = list(self.points)
        
        for i, (ts, val) in enumerate(points_list):
            # Get all points within window_size seconds before this point
            window_start = ts - window_size
            window_values = [
                v for t, v in points_list
                if window_start <= t <= ts
            ]
            
            if window_values:
                avg = statistics.mean(window_values)
                result.append((ts, avg))
        
        return result
    
    def get_recent_points(self, count: int) -> List[Tuple[float, float]]:
        """
        Get the most recent N points.
        
        Args:
            count: Number of recent points to return
        
        Returns:
            List of most recent (timestamp, value) tuples
        """
        points_list = list(self.points)
        return points_list[-count:] if count > 0 else []
    
    def clear(self) -> None:
        """Clear all data from the buffer."""
        self.points.clear()
        self.last_timestamp = None
    
    def get_point_count(self) -> int:
        """Return the number of points currently in the buffer."""
        return len(self.points)
    
    def get_memory_info(self) -> Dict[str, Any]:
        """
        Get information about buffer memory usage.
        
        Returns:
            Dictionary with memory stats
        """
        import sys
        
        if not self.points:
            return {
                'point_count': 0,
                'max_points': self.max_points,
                'estimated_memory_bytes': 0,
                'time_window_seconds': self.time_window,
            }
        
        # Rough estimate: each point is tuple of (float, float)
        # Each float is ~24 bytes, tuple overhead ~56 bytes
        bytes_per_point = 56 + 2 * 24
        estimated_bytes = len(self.points) * bytes_per_point
        
        min_ts, max_ts = self.get_time_range()
        time_span = (max_ts - min_ts) if (min_ts and max_ts) else 0
        
        return {
            'point_count': len(self.points),
            'max_points': self.max_points,
            'estimated_memory_bytes': estimated_bytes,
            'time_window_seconds': self.time_window,
            'actual_time_span_seconds': time_span,
            'bytes_per_point_estimate': bytes_per_point,
        }
