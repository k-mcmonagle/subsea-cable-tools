"""
Card Configuration Data Model

Defines the structure and serialization for live data cards.
Each card displays a single live value from the data stream.
"""

import json
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any


@dataclass
class CardConfig:
    """
    Configuration for a single live data card.
    
    Attributes:
        card_id: Unique identifier for the card (UUID)
        name: Display name for the card (e.g., "Ship Depth")
        field_name: Source field from data stream (e.g., "Depth_m")
        field_type: Type of field - "numeric" or "string" (default: "numeric")
        unit: Display unit (e.g., "m", "°", "knots")
        decimal_places: Number of decimal places to display (-1 for default)
        prefix: Text to display before value (e.g., "Depth: ")
        suffix: Text to display after value (e.g., " m")
        enabled: Whether card is actively displayed
        text_color: Hex color for text (e.g., "#FFFFFF")
        background_color: Hex color for background (e.g., "#000000")
        warning_min: Minimum value before warning (None for disabled, ignored for string fields)
        warning_max: Maximum value before warning (None for disabled, ignored for string fields)
        warning_color: Hex color for warning state (e.g., "#FF0000")
        alert_on_change: Show animation when value changes
        font_size: Font size in points (default 12)
    """
    card_id: str
    name: str
    field_name: str
    field_type: str = "numeric"  # "numeric" or "string"
    unit: str = ""
    decimal_places: int = -1  # -1 means use default
    prefix: str = ""
    suffix: str = ""
    enabled: bool = True
    text_color: str = "#000000"
    background_color: str = "#FFFFFF"
    warning_min: Optional[float] = None
    warning_max: Optional[float] = None
    warning_color: str = "#FF6600"
    alert_on_change: bool = False
    font_size: int = 12
    custom_data: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "CardConfig":
        """Create CardConfig from dictionary."""
        return CardConfig(**data)

    def to_dict(self) -> Dict[str, Any]:
        """Convert CardConfig to dictionary for JSON serialization."""
        return asdict(self)

    @staticmethod
    def from_json(json_str: str) -> "CardConfig":
        """Create CardConfig from JSON string."""
        data = json.loads(json_str)
        return CardConfig.from_dict(data)

    def to_json(self) -> str:
        """Convert CardConfig to JSON string."""
        return json.dumps(self.to_dict(), indent=2)

    def validate(self, available_fields: list) -> tuple[bool, str]:
        """
        Validate card configuration.
        
        Args:
            available_fields: List of available field names from data stream
            
        Returns:
            (is_valid, error_message)
        """
        if not self.name or not self.name.strip():
            return False, "Card name cannot be empty"
        
        if not self.field_name or not self.field_name.strip():
            return False, "Field name cannot be empty"
        
        if self.field_name not in available_fields:
            return False, f"Field '{self.field_name}' not found in data stream"
        
        if self.decimal_places < -1:
            return False, "Decimal places must be -1 (default) or >= 0"
        
        if self.warning_min is not None and self.warning_max is not None:
            if self.warning_min > self.warning_max:
                return False, "Warning minimum cannot be greater than maximum"
        
        return True, ""
