"""
Plot Configuration Data Model

Defines the structure and serialization for live data plots.
Each plot displays a time-series trend from the data stream.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List


@dataclass
class PlotStyling:
    """Styling configuration for plot appearance."""
    line_color: str = "#0066FF"
    line_width: int = 2
    fill_under_line: bool = False
    marker_style: str = "none"  # none|circle|square|diamond|cross
    marker_size: int = 4
    secondary_line_color: str = "#FF6600"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'PlotStyling':
        """Create from dictionary."""
        return PlotStyling(**data)


@dataclass
class AxisConfig:
    """Y-axis configuration."""
    auto_scale: bool = True
    y_min: Optional[float] = None
    y_max: Optional[float] = None
    show_grid: bool = True
    show_legend: bool = False
    scroll_right_to_left: bool = True  # New: enable right-to-left time scrolling
    x_axis_auto_scale: bool = True  # New: auto-scale x-axis vs fixed time window
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'AxisConfig':
        """Create from dictionary."""
        return AxisConfig(**data)


@dataclass
class AdvancedConfig:
    """Advanced plot configuration."""
    show_average_line: bool = False
    average_window: int = 60  # seconds
    alert_on_threshold: bool = False
    alert_min: Optional[float] = None
    alert_max: Optional[float] = None
    alert_color: str = "#FF0000"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'AdvancedConfig':
        """Create from dictionary."""
        return AdvancedConfig(**data)


@dataclass
class PlotConfig:
    """
    Configuration for a single live data plot.
    
    Attributes:
        plot_id: Unique identifier for the plot (UUID)
        name: Display name for the plot (e.g., "Depth Trend")
        field_names: List of 1-2 field names to display
        time_window: Duration to display in seconds (e.g., 300 for 5 minutes)
        max_points: Maximum number of data points to keep (for memory efficiency)
        update_interval: How often to refresh plot in milliseconds (100-5000)
        units: Display unit for primary Y-axis (e.g., "m", "knots")
        secondary_units: Display unit for secondary Y-axis (if dual-field)
        enabled: Whether plot is actively displayed
        styling: PlotStyling configuration
        axis_config: AxisConfig for Y-axis
        advanced: AdvancedConfig for advanced features
        custom_data: Dictionary for custom metadata
    """
    plot_id: str
    name: str
    field_names: List[str]  # e.g., ["Depth_m"] or ["Depth_m", "Heading_deg"]
    time_window: int = 300  # seconds
    max_points: int = 1000
    update_interval: int = 500  # milliseconds
    units: str = ""
    secondary_units: str = ""
    enabled: bool = True
    styling: PlotStyling = field(default_factory=PlotStyling)
    axis_config: AxisConfig = field(default_factory=AxisConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)
    custom_data: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'plot_id': self.plot_id,
            'name': self.name,
            'field_names': self.field_names,
            'time_window': self.time_window,
            'max_points': self.max_points,
            'update_interval': self.update_interval,
            'units': self.units,
            'secondary_units': self.secondary_units,
            'enabled': self.enabled,
            'styling': self.styling.to_dict(),
            'axis_config': self.axis_config.to_dict(),
            'advanced': self.advanced.to_dict(),
            'custom_data': self.custom_data,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'PlotConfig':
        """Create PlotConfig from dictionary."""
        return PlotConfig(
            plot_id=data.get('plot_id', ''),
            name=data.get('name', ''),
            field_names=data.get('field_names', []),
            time_window=data.get('time_window', 300),
            max_points=data.get('max_points', 1000),
            update_interval=data.get('update_interval', 500),
            units=data.get('units', ''),
            secondary_units=data.get('secondary_units', ''),
            enabled=data.get('enabled', True),
            styling=PlotStyling.from_dict(data.get('styling', {})),
            axis_config=AxisConfig.from_dict(data.get('axis_config', {})),
            advanced=AdvancedConfig.from_dict(data.get('advanced', {})),
            custom_data=data.get('custom_data', {}),
        )
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)
    
    @staticmethod
    def from_json(json_str: str) -> 'PlotConfig':
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return PlotConfig.from_dict(data)
