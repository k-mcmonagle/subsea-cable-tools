"""
Card Grid Widget

Display cards in a responsive grid layout with zoom control.
Auto-wraps columns based on available width.
"""

from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QSlider, QLabel, QPushButton, QSpinBox
)
from qgis.PyQt.QtCore import Qt, pyqtSignal

from .compact_card_widget import CompactCardWidget
from .card_config import CardConfig
from typing import Dict, List


class CardGridWidget(QWidget):
    """
    Grid display of compact cards with zoom control.
    
    Features:
    - Auto-wrapping grid layout
    - Zoom slider (75% - 150%)
    - Cards adjust size based on zoom
    - Smooth reflow when resizing
    - Right-click context menu for edit/delete
    """
    
    card_removed = pyqtSignal(str)  # card_id
    card_edit_requested = pyqtSignal(str)  # card_id
    card_delete_requested = pyqtSignal(str)  # card_id
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards: Dict[str, CompactCardWidget] = {}  # card_id -> widget
        self.zoom_level = 100  # Percent
        
        self.setup_ui()
    
    def setup_ui(self):
        """Build the grid with zoom controls."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(6)
        
        # Zoom controls (top toolbar)
        zoom_layout = QHBoxLayout()
        
        zoom_label = QLabel("Zoom:")
        zoom_layout.addWidget(zoom_label)
        
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setMinimum(75)
        self.zoom_slider.setMaximum(150)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setTickPosition(QSlider.TicksBelow)
        self.zoom_slider.setTickInterval(25)
        self.zoom_slider.setMaximumWidth(200)
        self.zoom_slider.valueChanged.connect(self.on_zoom_changed)
        zoom_layout.addWidget(self.zoom_slider)
        
        self.zoom_percent_label = QLabel("100%")
        self.zoom_percent_label.setMinimumWidth(40)
        zoom_layout.addWidget(self.zoom_percent_label)
        
        zoom_layout.addStretch()
        main_layout.addLayout(zoom_layout)
        
        # Grid layout for cards (scrollable area will be added by parent)
        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(4)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        
        main_layout.addWidget(self.grid_widget)
        main_layout.addStretch()
    
    def add_card(self, config: CardConfig) -> CompactCardWidget:
        """
        Add a card to the grid.
        
        Args:
            config: CardConfig for the new card
            
        Returns:
            The created CompactCardWidget
        """
        card_widget = CompactCardWidget(config)
        card_widget.set_zoom(self.zoom_level)
        
        # Connect signals
        card_widget.card_edit_requested.connect(self.card_edit_requested.emit)
        card_widget.card_delete_requested.connect(self.on_card_delete_requested)
        
        # Add to grid at next position
        row = len(self.cards) // self.get_columns()
        col = len(self.cards) % self.get_columns()
        self.grid_layout.addWidget(card_widget, row, col)
        
        self.cards[config.card_id] = card_widget
        return card_widget
    
    def remove_card(self, card_id: str):
        """Remove a card from the grid."""
        if card_id in self.cards:
            widget = self.cards[card_id]
            widget.setParent(None)
            del self.cards[card_id]
            self.reflow_grid()
            self.card_removed.emit(card_id)
    
    def on_card_delete_requested(self, card_id: str):
        """Handle delete request from card context menu."""
        self.remove_card(card_id)
    
    def get_card(self, card_id: str) -> CompactCardWidget:
        """Get card widget by ID."""
        return self.cards.get(card_id)
    
    def update_card_value(self, card_id: str, value):
        """Update value for a specific card."""
        if card_id in self.cards:
            self.cards[card_id].update_value(value)
    
    def reflow_grid(self):
        """
        Reflow grid to adapt to new dimensions.
        Call this after adding/removing cards or resizing.
        """
        # Clear current layout
        while self.grid_layout.count():
            item = self.grid_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        
        # Re-add cards in order
        columns = self.get_columns()
        for idx, card_widget in enumerate(self.cards.values()):
            row = idx // columns
            col = idx % columns
            self.grid_layout.addWidget(card_widget, row, col)
    
    def get_columns(self) -> int:
        """
        Calculate optimal number of columns based on available width.
        
        Returns:
            Number of columns (minimum 1)
        """
        if self.grid_widget.width() <= 0:
            return 3  # Default fallback
        
        # Each card is ~80-200px wide depending on zoom
        card_width = int(80 * (self.zoom_level / 100.0)) + 8  # +8 for spacing
        available_width = self.grid_widget.width()
        
        columns = max(1, available_width // card_width)
        return columns
    
    def on_zoom_changed(self, value: int):
        """Handle zoom slider change."""
        self.zoom_level = value
        self.zoom_percent_label.setText(f"{value}%")
        
        # Update all cards
        for card in self.cards.values():
            card.set_zoom(value)
        
        # Reflow if column count changed
        self.reflow_grid()
    
    def resizeEvent(self, event):
        """Handle widget resize - reflow grid if needed."""
        super().resizeEvent(event)
        # Reflow grid to adapt to new width
        self.reflow_grid()
    
    def get_all_cards(self) -> List[CompactCardWidget]:
        """Get all card widgets."""
        return list(self.cards.values())
    
    def clear_all_cards(self):
        """Remove all cards from grid."""
        card_ids = list(self.cards.keys())
        for card_id in card_ids:
            self.remove_card(card_id)
