"""
Compact Card Widget for Grid Display

A minimal 3-line card design optimized for grid layouts with many cards.
Shows label, value, and unit in a compact form.
"""

from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame, QMenu
from qgis.PyQt.QtCore import Qt, pyqtSignal, QTimer, QPoint
from qgis.PyQt.QtGui import QFont, QColor

from .card_config import CardConfig
from typing import Any, Optional


class CompactCardWidget(QFrame):
    """
    Minimal card widget for grid display.
    
    Layout (3 lines):
    ┌─────────────┐
    │ Card Name   │ (9pt, bold)
    │ 1234.5      │ (14pt, bold, centered)
    │ m           │ (7pt, centered)
    └─────────────┘
    
    Min size: 80px wide x 60px tall
    Max size: 200px wide (flexible)
    
    Right-click context menu:
    - Edit Card
    - Delete Card
    """
    
    value_changed = pyqtSignal(str)
    warning_state_changed = pyqtSignal(bool)
    card_edit_requested = pyqtSignal(str)  # card_id
    card_delete_requested = pyqtSignal(str)  # card_id
    
    def __init__(self, config: CardConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.current_value: Optional[Any] = None  # Can be float or str depending on field_type
        self.in_warning_state = False
        self.zoom_level = 1.0  # 1.0 = 100%
        
        self.setup_ui()
        self.apply_styling()
    
    def setup_ui(self):
        """Build the compact 3-line layout."""
        self.setFrameShape(QFrame.StyledPanel)
        self.setFrameShadow(QFrame.Raised)
        self.setLineWidth(1)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)  # Minimal margins
        layout.setSpacing(1)  # Minimal space between elements
        
        # Line 1: Card Name (small, bold)
        self.name_label = QLabel(self.config.name)
        name_font = QFont()
        name_font.setPointSize(9)
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        self.name_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.name_label)
        
        # Line 2: Value (large, bold, centered)
        self.value_label = QLabel("--")
        value_font = QFont()
        value_font.setPointSize(14)
        value_font.setBold(True)
        self.value_label.setFont(value_font)
        self.value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label)
        
        # Line 3: Unit (tiny, centered)
        unit_text = f"{self.config.suffix}{self.config.unit}".strip()
        self.unit_label = QLabel(unit_text)
        unit_font = QFont()
        unit_font.setPointSize(7)
        self.unit_label.setFont(unit_font)
        self.unit_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.unit_label)
        
        # Minimal sizing
        self.setMinimumHeight(60)
        self.setMinimumWidth(80)
        self.setMaximumWidth(200)
    
    def apply_styling(self):
        """Apply colors from config."""
        bg_color = self.config.background_color
        text_color = self.config.text_color
        
        stylesheet = f"""
            CompactCardWidget {{
                background-color: {bg_color};
                border: 2px solid #CCCCCC;
                border-radius: 2px;
            }}
            QLabel {{
                color: {text_color};
            }}
        """
        self.setStyleSheet(stylesheet)
    
    def update_value(self, value: Any):
        """
        Update the card with a new value.
        
        Args:
            value: The value to display
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
                self.animate_update()
                return
            
            # Handle numeric fields
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
                formatted = f"{self.current_value:.3g}"
            else:
                formatted = f"{self.current_value:.{self.config.decimal_places}f}"
            
            self.value_label.setText(formatted)
            
            # Check warning conditions
            self.check_warning_state()
            
            # Emit signal
            self.value_changed.emit(formatted)
            
            # Animate update indicator
            self.animate_update()
            
        except (ValueError, TypeError):
            self.value_label.setText("ERR")
            self.current_value = None
            self.clear_warning_state()
    
    def check_warning_state(self):
        """Check if value is in warning range. Skip for string fields."""
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
        """Update styling for warning state."""
        if self.in_warning_state:
            bg_color = self.config.warning_color
            text_color = "#FFFFFF"
            border_color = "#FF0000"
        else:
            bg_color = self.config.background_color
            text_color = self.config.text_color
            border_color = "#CCCCCC"
        
        stylesheet = f"""
            CompactCardWidget {{
                background-color: {bg_color};
                border: 2px solid {border_color};
                border-radius: 2px;
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
    
    def animate_update(self):
        """Brief visual indicator of update."""
        # Change background to slightly brighter for 300ms
        original_bg = self.config.background_color
        if self.in_warning_state:
            highlight_bg = self.config.warning_color
        else:
            highlight_bg = original_bg
        
        # Could add animation here if desired
        # For now, just pulse the border
        QTimer.singleShot(300, lambda: None)  # Placeholder
    
    def set_zoom(self, zoom_percent: float):
        """
        Scale card size and fonts.
        
        Args:
            zoom_percent: 75-150 (where 100 = normal size)
        """
        self.zoom_level = zoom_percent / 100.0
        
        # Scale dimensions
        min_h = int(60 * self.zoom_level)
        min_w = int(80 * self.zoom_level)
        max_w = int(200 * self.zoom_level)
        
        self.setMinimumHeight(min_h)
        self.setMinimumWidth(min_w)
        self.setMaximumWidth(max_w)
        
        # Scale fonts
        name_font = QFont()
        name_font.setPointSize(int(9 * self.zoom_level))
        name_font.setBold(True)
        self.name_label.setFont(name_font)
        
        value_font = QFont()
        value_font.setPointSize(int(14 * self.zoom_level))
        value_font.setBold(True)
        self.value_label.setFont(value_font)
        
        unit_font = QFont()
        unit_font.setPointSize(int(7 * self.zoom_level))
        self.unit_label.setFont(unit_font)
    
    def get_value(self) -> Optional[float]:
        """Get current numeric value."""
        return self.current_value
    
    def get_formatted_value(self) -> str:
        """Get formatted display value."""
        return self.value_label.text()
    
    def contextMenuEvent(self, event):
        """Show context menu on right-click."""
        menu = QMenu(self)
        
        edit_action = menu.addAction("Edit Card")
        delete_action = menu.addAction("Delete Card")
        
        action = menu.exec_(self.mapToGlobal(event.pos()))
        
        if action == edit_action:
            self.card_edit_requested.emit(self.config.card_id)
        elif action == delete_action:
            self.card_delete_requested.emit(self.config.card_id)
