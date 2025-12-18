"""
Live Data Table Configuration Data Model

Defines the structure for live data table display.
Each table displays multiple live values in a compact table format.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List


@dataclass
class TableFieldConfig:
    """Configuration for a single field in the table."""
    field_name: str
    display_name: str = ""  # Custom display name (defaults to field_name if empty)
    unit: str = ""
    decimal_places: int = -1  # -1 for auto
    order: int = 0  # For ordering fields in table
    enabled: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'TableFieldConfig':
        """Create from dictionary."""
        return TableFieldConfig(**data)


@dataclass
class TableStyling:
    """Styling configuration for table appearance."""
    header_background: str = "#E0E0E0"
    header_text_color: str = "#000000"
    row_background: str = "#FFFFFF"
    row_alternate_background: str = "#F5F5F5"
    row_text_color: str = "#000000"
    border_color: str = "#CCCCCC"
    row_height: int = 24  # pixels
    font_size: int = 10  # points
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'TableStyling':
        """Create from dictionary."""
        return TableStyling(**data)


@dataclass
class TableConfig:
    """
    Configuration for a live data table display.
    
    Attributes:
        table_id: Unique identifier for the table (UUID)
        name: Display name for the table
        field_configs: List of TableFieldConfig for each field to display
        enabled: Whether table is actively displayed
        styling: TableStyling configuration
        show_headers: Whether to show column headers
        alternating_rows: Whether to alternate row background colors
        custom_data: Dictionary for custom metadata
    """
    table_id: str
    name: str
    field_configs: List[TableFieldConfig] = field(default_factory=list)
    enabled: bool = True
    styling: TableStyling = field(default_factory=TableStyling)
    show_headers: bool = True
    alternating_rows: bool = True
    custom_data: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'table_id': self.table_id,
            'name': self.name,
            'field_configs': [fc.to_dict() for fc in self.field_configs],
            'enabled': self.enabled,
            'styling': self.styling.to_dict(),
            'show_headers': self.show_headers,
            'alternating_rows': self.alternating_rows,
            'custom_data': self.custom_data,
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'TableConfig':
        """Create TableConfig from dictionary."""
        field_configs = [
            TableFieldConfig.from_dict(fc) for fc in data.get('field_configs', [])
        ]
        return TableConfig(
            table_id=data.get('table_id', ''),
            name=data.get('name', ''),
            field_configs=field_configs,
            enabled=data.get('enabled', True),
            styling=TableStyling.from_dict(data.get('styling', {})),
            show_headers=data.get('show_headers', True),
            alternating_rows=data.get('alternating_rows', True),
            custom_data=data.get('custom_data', {}),
        )
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2)
    
    @staticmethod
    def from_json(json_str: str) -> 'TableConfig':
        """Deserialize from JSON string."""
        data = json.loads(json_str)
        return TableConfig.from_dict(data)
    
    def get_enabled_fields(self) -> List[TableFieldConfig]:
        """Get only enabled fields, sorted by order."""
        return sorted(
            [fc for fc in self.field_configs if fc.enabled],
            key=lambda x: x.order
        )
    
    def reorder_fields(self, field_names: List[str]) -> None:
        """Reorder fields based on provided list."""
        # Create a map for quick lookup
        order_map = {name: idx for idx, name in enumerate(field_names)}
        
        # Update order in all configs
        for fc in self.field_configs:
            if fc.field_name in order_map:
                fc.order = order_map[fc.field_name]
