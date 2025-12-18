"""
Live Data Card Widget

Displays a single live value with label, unit, and optional warning indicators.
Updates in real-time as data is received.
"""

from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer, QPropertyAnimation, QRect, QSize
from qgis.PyQt.QtGui import QFont, QColor
from qgis.PyQt.QtCore import QEasingCurve

from .card_config import CardConfig
from typing import Any, Optional


class QCardWidget(QFrame):
    """
    Display widget for a single live data card.
    Shows label, current value, and unit with optional warning styling.
    """
    
    value_changed = pyqtSignal(str)  # Emits formatted value
    warning_state_changed = pyqtSignal(bool)  # Emits whether in warning state
    
    def __init__(self, config: CardConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.current_value: Optional[Any] = None  # Can be float or str depending on field_type
        self.in_warning_state = False
        self.last_update_time = None
        
        self.setup_ui()
        self.apply_styling()
        
    def setup_ui(self):
        """Build the card UI layout."""
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)
        self.setLineWidth(1)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        
        # Header: Card name
        header_layout = QHBoxLayout()
        self.name_label = QLabel(self.config.name)
        name_font = QFont()
        name_font.setPointSize(10)
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        self.name_label.setAlignment(Qt.AlignLeft)
        header_layout.addWidget(self.name_label)
        header_layout.addStretch()
        
        # Update indicator (small dot)
        self.update_indicator = QLabel("●")
        self.update_indicator.setStyleSheet("color: #AAAAAA; font-size: 6px;")
        self.update_indicator.setAlignment(Qt.AlignRight | Qt.AlignTop)
        header_layout.addWidget(self.update_indicator)
        
        layout.addLayout(header_layout)
        
        # Main value display
        value_layout = QHBoxLayout()
        
        # Prefix text
        if self.config.prefix:
            self.prefix_label = QLabel(self.config.prefix)
            self.prefix_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value_layout.addWidget(self.prefix_label)
        
        # Value
        self.value_label = QLabel("--")
        value_font = QFont()
        value_font.setPointSize(self.config.font_size)
        value_font.setBold(True)
        self.value_label.setFont(value_font)
        self.value_label.setAlignment(Qt.AlignCenter)
        value_layout.addWidget(self.value_label)
        
        # Suffix + unit
        if self.config.unit or self.config.suffix:
            suffix_text = f"{self.config.suffix}{self.config.unit}".strip()
            self.suffix_label = QLabel(suffix_text)
            self.suffix_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            suffix_font = QFont()
            suffix_font.setPointSize(self.config.font_size - 2)
            self.suffix_label.setFont(suffix_font)
            value_layout.addWidget(self.suffix_label)
        
        layout.addLayout(value_layout)
        
        # Set minimum size
        self.setMinimumWidth(140)
        self.setMinimumHeight(100)
        
    def apply_styling(self):
        """Apply colors and styling from config."""
        bg_color = self.config.background_color
        text_color = self.config.text_color
        
        # Build stylesheet
        stylesheet = f"""
            QCardWidget {{
                background-color: {bg_color};
                border: 2px solid #CCCCCC;
                border-radius: 4px;
            }}
            QLabel {{
                color: {text_color};
            }}
        """
        self.setStyleSheet(stylesheet)
    
    def update_value(self, value: Any):
        """
        Update the card with a new value from the data stream.
        
        Args:
            value: The value to display (numeric or string depending on field_type)
        """
        try:
            # Handle string fields
            if self.config.field_type == "string":
                if value is None:
                    self.value_label.setText("--")
                    self.current_value = None
                else:
                    text_value = str(value).strip()
                    self.value_label.setText(text_value if text_value else "--")
                    self.current_value = text_value
                
                # No warning state for string fields
                self.clear_warning_state()
                self.value_changed.emit(str(self.current_value))
                self.animate_update_indicator()
                return
            
            # Handle numeric fields (original logic)
            # Convert to float for numeric operations
            if isinstance(value, str):
                value = float(value.strip()) if value.strip() else None
            
            if value is None:
                self.value_label.setText("--")
                self.current_value = None
                self.clear_warning_state()
                return
            
            self.current_value = float(value)
            
            # Format the value
            if self.config.decimal_places == -1:
                # Auto format
                formatted = f"{self.current_value:.3g}"  # 3 significant figures
            else:
                formatted = f"{self.current_value:.{self.config.decimal_places}f}"
            
            self.value_label.setText(formatted)
            
            # Check warning conditions
            self.check_warning_state()
            
            # Emit change signal
            self.value_changed.emit(formatted)
            
            # Animate update indicator
            self.animate_update_indicator()
            
        except (ValueError, TypeError) as e:
            self.value_label.setText("ERR")
            self.current_value = None
            self.clear_warning_state()
    
    def check_warning_state(self):
        """Check if current value is in warning range and update styling."""
        # Skip warning checks for string fields or None values
        if self.current_value is None or isinstance(self.current_value, str):
            self.clear_warning_state()
            return
        
        in_warning = False
        
        if self.config.warning_min is not None and self.current_value < self.config.warning_min:
            in_warning = True
        
        if self.config.warning_max is not None and self.current_value > self.config.warning_max:
            in_warning = True
        
        if in_warning != self.in_warning_state:
            self.in_warning_state = in_warning
            self.update_warning_styling()
            self.warning_state_changed.emit(in_warning)
    
    def update_warning_styling(self):
        """Update styling to reflect warning state."""
        if self.in_warning_state:
            # Apply warning styling
            bg_color = self.config.warning_color
            text_color = "#FFFFFF"  # White text on warning color
            border_color = "#FF0000"
        else:
            # Reset to normal styling
            bg_color = self.config.background_color
            text_color = self.config.text_color
            border_color = "#CCCCCC"
        
        stylesheet = f"""
            QCardWidget {{
                background-color: {bg_color};
                border: 2px solid {border_color};
                border-radius: 4px;
            }}
            QLabel {{
                color: {text_color};
            }}
        """
        self.setStyleSheet(stylesheet)
    
    def clear_warning_state(self):
        """Clear warning state and reset styling."""
        if self.in_warning_state:
            self.in_warning_state = False
            self.apply_styling()
            self.warning_state_changed.emit(False)
    
    def animate_update_indicator(self):
        """Pulse the update indicator to show data received."""
        # Change color to green, then fade back
        self.update_indicator.setStyleSheet("color: #00AA00; font-size: 6px;")
        
        # Schedule reset after 500ms
        QTimer.singleShot(500, lambda: self.update_indicator.setStyleSheet(
            "color: #AAAAAA; font-size: 6px;"
        ))
    
    def set_enabled(self, enabled: bool):
        """Enable or disable the card."""
        self.config.enabled = enabled
        self.setEnabled(enabled)
        if not enabled:
            self.setStyleSheet(self.styleSheet() + "\n opacity: 0.5;")
    
    def get_value(self) -> Optional[float]:
        """Get the current numeric value."""
        return self.current_value
    
    def get_formatted_value(self) -> str:
        """Get the formatted string value as displayed."""
        return self.value_label.text()
